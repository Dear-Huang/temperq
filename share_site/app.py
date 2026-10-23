"""M4 share site (SPEC §15): minimal, metadata and links only.

- Login: Hugging Face OAuth. Without HF credentials the app runs in local dev
  mode, where /login/dev signs in a fixed user and only accepts requests from
  127.0.0.1. Dev mode must never be exposed publicly.
- Share: user pastes an HF model repo id/URL. The server fetches README.md at a
  pinned commit through the HF HTTP API, parses the YAML front matter with
  qwb.card.parse (safe_load, escaped values) and stores metadata only. No repo
  code is downloaded or executed.
- Pages: two columns, tool report fields vs. author text (rendered as escaped
  plain text, not HTML). Source gone -> "源文件已不可用".

Server:  HF_OAUTH_CLIENT_ID=... HF_OAUTH_CLIENT_SECRET=... HF_OAUTH_REDIRECT_URI=...
         QWB_SITE_SECRET=... python -X utf8 -m uvicorn share_site.app:app --port 8800
Local:   QWB_SITE_DEV=1 python -X utf8 -m uvicorn share_site.app:app --port 8800
The app refuses to start with neither set.
"""

from __future__ import annotations

import json
import html
import math
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from jinja2 import Environment  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from qwb import card  # noqa: E402

HF = "https://huggingface.co"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
MAX_README = 200_000

env = Environment(autoescape=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_id(text: str) -> str | None:
    t = text.strip()
    if t.startswith(HF + "/"):
        t = t[len(HF) + 1:]
    t = t.split("?")[0].split("#")[0].strip("/")
    parts = t.split("/")
    t = "/".join(parts[:2])
    return t if REPO_RE.match(t) and ".." not in t else None


class HFClient:
    """Reads only public metadata and README.md; never code or weights."""

    def __init__(self, proxy: str | None = None, timeout: float = 20):
        self.http = httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=False)

    def commit(self, repo: str) -> str | None:
        r = self.http.get(f"{HF}/api/models/{repo}")
        if r.status_code != 200:
            return None
        sha = r.json().get("sha")
        return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def readme(self, repo: str, sha: str) -> str | None:
        r = self.http.get(f"{HF}/{repo}/raw/{sha}/README.md")
        if r.status_code != 200 or len(r.content) > MAX_README:
            return None
        return r.text


def db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript("""
    create table if not exists shares(
      id integer primary key, owner text not null, repo text not null, sha text not null,
      fields text not null, missing text not null, flags text not null, author_text text,
      shared_at text not null, status text not null default 'ok', checked_at text,
      deprecated integer not null default 0, unique(repo, sha));
    """)
    if "research" not in {r[1] for r in con.execute("pragma table_info(shares)")}:
        con.execute("alter table shares add column research text not null default '{}'")
        con.commit()
    return con


class RateLimit:
    """Sliding-window counters: {name: (max_events, window_seconds)}."""

    def __init__(self, rules: dict, clock: Callable[[], float] = None):
        import collections
        import threading
        import time as _time
        self.rules, self.clock = rules, clock or _time.monotonic
        self.events = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def hit(self, keys: list[str]) -> int:
        """Record one event for every key; return seconds to wait if any key is over a limit."""
        now = self.clock()
        with self.lock:
            wait = 0
            for k in keys:
                q = self.events[k]
                horizon = max(w for _, w in self.rules.values())
                while q and now - q[0] > horizon:
                    q.popleft()
                for n, w in self.rules.values():
                    recent = [t for t in q if now - t <= w]
                    if len(recent) >= n:
                        wait = max(wait, int(w - (now - recent[0])) + 1)
            if not wait:
                for k in keys:
                    self.events[k].append(now)
            return wait


def _client_ip(request: Request) -> str:
    """Client address; behind a proxy, the last hop the proxy appended (not client-controlled)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd and os.environ.get("QWB_SITE_TRUST_PROXY") == "1":
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def create_app(db_path: Path | None = None, hf: HFClient | None = None,
               oauth: dict | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    web_dist = ROOT / "website" / "dist"
    app.mount("/assets", StaticFiles(directory=web_dist / "assets", check_dir=False), name="assets")
    app.mount("/brands", StaticFiles(directory=ROOT / "website" / "public" / "brands", check_dir=False), name="brands")
    app.mount("/fonts", StaticFiles(directory=ROOT / "website" / "public" / "fonts", check_dir=False), name="fonts")
    if oauth is None:
        # configured from the environment (the uvicorn entry point). Without OAuth the
        # site would fall back to the passwordless dev login; behind a reverse proxy
        # every request looks like 127.0.0.1, so that must be an explicit choice.
        if os.environ.get("HF_OAUTH_CLIENT_ID"):
            oauth = {"client_id": os.environ["HF_OAUTH_CLIENT_ID"],
                     "client_secret": os.environ["HF_OAUTH_CLIENT_SECRET"],
                     "redirect_uri": os.environ["HF_OAUTH_REDIRECT_URI"]}
        elif os.environ.get("QWB_SITE_DEV") == "1":
            oauth = {}
        else:
            raise RuntimeError(
                "share site needs HF OAuth: set HF_OAUTH_CLIENT_ID, HF_OAUTH_CLIENT_SECRET and "
                "HF_OAUTH_REDIRECT_URI. For local testing only, set QWB_SITE_DEV=1.")
    secret = secret or os.environ.get("QWB_SITE_SECRET")
    if not secret:
        if oauth:
            raise RuntimeError("set QWB_SITE_SECRET so sessions survive restarts and are not guessable")
        secret = secrets.token_urlsafe(32)
    # oauth is resolved by now, so production cookies are HTTPS-only
    # writes (share, deprecate, forum posts and replies) are rate-limited per signed-in
    # user and per client address; reads are not. In-memory: resets on restart, which
    # is fine for one process behind a proxy.
    limits = RateLimit({"short": (10, 60), "day": (200, 86400)})

    @app.middleware("http")
    async def write_limit(request: Request, call_next):
        if request.method == "POST":
            who = [f"ip:{_client_ip(request)}"]
            sess = request.scope.get("session") or {}
            if sess.get("user"):
                who.append(f"u:{sess['user']}")
            wait = limits.hit(who)
            if wait:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": f"操作太频繁，请 {wait} 秒后再试"}, status_code=429,
                                    headers={"Retry-After": str(wait)})
        return await call_next(request)

    # added after the limiter so it wraps it: the limiter can read the session
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax",
                       https_only=bool(oauth))
    database = Path(db_path or os.environ.get("QWB_SITE_DB") or ROOT / "runs" / "site.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    con = db(database)
    hf = hf or HFClient(proxy=os.environ.get("HTTPS_PROXY"))

    @app.get("/healthz")
    def health():
        con.execute("select 1").fetchone()
        return {"status": "ok"}

    def user(req: Request) -> str | None:
        return req.session.get("user")

    def page(title: str, body: str, req: Request, **kw) -> HTMLResponse:
        t = env.from_string((ROOT / "share_site" / "templates" / "layout.html").read_text(encoding="utf-8"))
        return HTMLResponse(t.render(title=title, body=env.from_string(body).render(**kw),
                                     user=user(req), dev=not oauth))

    def csrf(req: Request) -> str:
        if "csrf" not in req.session:
            req.session["csrf"] = secrets.token_urlsafe(16)
        return req.session["csrf"]

    def check_csrf(req: Request, token: str) -> None:
        if not token or token != req.session.get("csrf"):
            raise HTTPException(403, "bad csrf token")

    # ---------------------------------------------------------------- auth
    def return_path(value: str) -> str:
        return value if re.fullmatch(r"/community(?:/new|/t/\d+)?", value) else "/"

    @app.get("/login")
    def login(req: Request, next: str = "/"):
        req.session["return_to"] = return_path(next)
        if not oauth:
            return RedirectResponse("/login/dev", 302)
        state = secrets.token_urlsafe(16)
        req.session["oauth_state"] = state
        q = urllib.parse.urlencode({"client_id": oauth["client_id"],
                                    "redirect_uri": oauth["redirect_uri"],
                                    "scope": "openid profile", "response_type": "code",
                                    "state": state})
        return RedirectResponse(f"{HF}/oauth/authorize?{q}", 302)

    @app.get("/oauth/callback")
    def callback(req: Request, code: str = "", state: str = ""):
        if not oauth or not state or state != req.session.pop("oauth_state", None):
            raise HTTPException(400, "bad oauth state")
        tok = hf.http.post(f"{HF}/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": oauth["redirect_uri"]},
            auth=(oauth["client_id"], oauth["client_secret"]))
        if tok.status_code != 200:
            raise HTTPException(400, "oauth token exchange failed")
        info = hf.http.get(f"{HF}/oauth/userinfo",
                           headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        name = info.json().get("preferred_username") if info.status_code == 200 else None
        if not name:
            raise HTTPException(400, "no username from HF")
        destination = return_path(req.session.get("return_to", "/"))
        req.session.clear()
        req.session["user"] = str(name)[:96]          # the only personal datum stored
        return RedirectResponse(destination, 302)

    @app.get("/login/dev")
    def login_dev(req: Request):
        if oauth or (req.client and req.client.host not in ("127.0.0.1", "::1", "testclient")):
            raise HTTPException(404)
        # a reverse proxy makes every visitor look local; forwarded requests never qualify
        if any(h in req.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded")):
            raise HTTPException(404)
        req.session["user"] = "local-dev"
        return RedirectResponse(return_path(req.session.pop("return_to", "/")), 302)

    @app.get("/logout")
    def logout(req: Request):
        req.session.clear()
        return RedirectResponse("/", 302)

    # ---------------------------------------------------------------- pages
    def shared_models():
        rows = con.execute(
            "select id, owner, repo, sha, fields, author_text, research, shared_at, status, deprecated, flags "
            "from shares order by id desc limit 200").fetchall()
        return [dict(r) | {"fields": json.loads(r["fields"]), "flags": json.loads(r["flags"]),
                           "research": json.loads(r["research"])}
                for r in rows]

    @app.get("/api/site")
    def site_data(req: Request):
        return {"user": user(req), "csrf": csrf(req), "dev": not bool(oauth),
                "models": shared_models()}

    def reference_report() -> Path:
        return ROOT / "share_site" / "reference" / "report.md"

    @app.get("/api/reference")
    def reference_data():
        return {"report": reference_report().read_text(encoding="utf-8")}

    @app.get("/downloads/report")
    def download_report():
        return FileResponse(reference_report(), filename="wisehold-reference-report.md",
                            media_type="text/markdown; charset=utf-8")

    @app.get("/downloads/recipe")
    def download_recipe():
        return FileResponse(ROOT / "examples" / "run_cn_rank_5d.yaml",
                            filename="run_cn_rank_5d.yaml", media_type="application/yaml")

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(ROOT / "website" / "public" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/hero-poster.png")
    def hero_poster():
        path = ROOT / "website" / "public" / "hero-poster.png"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.get("/", response_class=HTMLResponse)
    def index(req: Request):
        if (web_dist / "index.html").exists():
            source = (web_dist / "index.html").read_text(encoding="utf-8")
            fallback = '<input type="hidden" name="token" value="' + html.escape(csrf(req)) + '">'
            fallback += "<noscript><h1>WiseHold</h1><p>请启用 JavaScript 以浏览模型广场。</p>"
            for m in shared_models():
                status = "源文件已不可用" if m["status"] == "gone" else "已弃用" if m["deprecated"] else ""
                fallback += f'<p><a href="/m/{m["id"]}">{html.escape(m["repo"])}</a> {status}</p>'
            fallback += "</noscript>"
            return HTMLResponse(source.replace("</body>", fallback + "</body>"),
                                headers={"Cache-Control": "no-store"})
        rows = con.execute("select id, owner, repo, sha, fields, status, deprecated, flags "
                           "from shares order by id desc limit 200").fetchall()
        items = [dict(r) | {"f": json.loads(r["fields"]), "fl": json.loads(r["flags"])}
                 for r in rows]
        return page("模型分享", INDEX, req, items=items, csrf=csrf(req))

    @app.post("/share")
    def share(req: Request, repo: str = Form(...), author_text: str = Form(""),
              token: str = Form(""), recipe_text: str = Form("", max_length=8000),
              test_period: str = Form("", max_length=80), rank_ic: str = Form(""),
              net_excess_ann: str = Form("")):
        u = user(req)
        if not u:
            raise HTTPException(401, "login first")
        check_csrf(req, token)
        research = {"recipe": recipe_text, "test_period": test_period}
        for name, value, lower, upper in (("rank_ic", rank_ic, -1, 1),
                                          ("net_excess_ann", net_excess_ann, -100, 10000)):
            if value.strip():
                try:
                    number = float(value)
                except ValueError:
                    raise HTTPException(422, f"{name} 必须是有效数值")
                if not math.isfinite(number) or not lower <= number <= upper:
                    raise HTTPException(422, f"{name} 超出有效范围")
                research[name] = number
        if ("rank_ic" in research or "net_excess_ann" in research) and not test_period.strip():
            raise HTTPException(422, "填写业绩指标时，请同时注明样本外测试区间")
        rid = repo_id(repo)
        if not rid:
            raise HTTPException(400, "not a Hugging Face model repo id")
        sha = hf.commit(rid)
        if not sha:
            raise HTTPException(400, "repo not found or not public")
        text = hf.readme(rid, sha)
        if text is None:
            raise HTTPException(400, "README.md missing or too large")
        p = card.parse(text)
        if not p["ok"]:
            raise HTTPException(400, f"model card: {p['error']}")
        con.execute(
            "insert or ignore into shares(owner, repo, sha, fields, missing, flags, author_text,"
            " shared_at, checked_at, research) values (?,?,?,?,?,?,?,?,?,?)",
            (u, rid, sha, json.dumps(p["fields"], ensure_ascii=False),
             json.dumps(p["missing"]), json.dumps(p["flags"], ensure_ascii=False),
             author_text[:5000], _now(), _now(), json.dumps(research, ensure_ascii=False)))
        con.commit()
        sid = con.execute("select id from shares where repo=? and sha=?", (rid, sha)).fetchone()[0]
        return RedirectResponse(f"/m/{sid}", 303)

    @app.get("/m/{sid}", response_class=HTMLResponse)
    def model(req: Request, sid: int):
        r = con.execute("select * from shares where id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404)
        versions = con.execute("select id, sha, shared_at, deprecated, status from shares "
                               "where repo=? order by id", (r["repo"],)).fetchall()
        return page(r["repo"], MODEL, req, r=dict(r), f=json.loads(r["fields"]),
                    missing=json.loads(r["missing"]), flags=json.loads(r["flags"]),
                    research=json.loads(r["research"]),
                    versions=versions, csrf=csrf(req), owner=user(req) == r["owner"])

    @app.post("/m/{sid}/deprecate")
    def deprecate(req: Request, sid: int, token: str = Form("")):
        check_csrf(req, token)
        r = con.execute("select owner from shares where id=?", (sid,)).fetchone()
        if not r or r["owner"] != user(req):
            raise HTTPException(403)
        con.execute("update shares set deprecated=1 where id=?", (sid,))   # never deleted
        con.commit()
        return RedirectResponse(f"/m/{sid}", 303)

    def recheck(sid: int) -> str:
        r = con.execute("select repo, sha from shares where id=?", (sid,)).fetchone()
        ok = hf.readme(r["repo"], r["sha"]) is not None
        st = "ok" if ok else "gone"
        con.execute("update shares set status=?, checked_at=? where id=?", (st, _now(), sid))
        con.commit()
        return st

    app.state.recheck = recheck
    app.state.db = con
    from share_site.forum import register_forum
    register_forum(app, con, user, csrf, check_csrf, _now, index)
    return app


LAYOUT = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--acc:#2f5d8a;--warn:#9a3b1c}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#2e2e2b;--acc:#8fb3d9;--warn:#e08a6a}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;margin:0;padding:0 16px}
main{max-width:1000px;margin:0 auto;padding-block:24px}
a{color:var(--acc)} header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}@media(max-width:720px){.cols{grid-template-columns:1fr}}
.box{border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.mut{color:var(--mut)} .warn{color:var(--warn)} table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left;vertical-align:top}
input,textarea,button{font:inherit;padding:6px 8px;max-width:100%;box-sizing:border-box}
pre{white-space:pre-wrap;word-break:break-word;margin:0}
</style></head><body><main>
<header><strong><a href="/">WiseHold 模型分享</a></strong><span>
{% if user %}{{ user }} · <a href="/logout">退出</a>{% else %}<a href="/login">用 Hugging Face 登录</a>{% endif %}
</span></header>
{% if dev %}<p class="warn">本地开发模式：未配置 HF OAuth，仅限 127.0.0.1 访问，不可公开部署。</p>{% endif %}
<p class="mut">技术分享平台。不排名、不审核、不承诺收益，不构成投资建议。</p>
{{ body|safe }}
</main></body></html>"""

INDEX = """
<form method="post" action="/share" class="box">
<input type="hidden" name="token" value="{{ csrf }}">
<label>Hugging Face 模型仓库（如 user/model）<br><input name="repo" size="50" required></label><br>
<label>作者自述（纯文本，展示时不渲染 HTML）<br><textarea name="author_text" rows="3" cols="60"></textarea></label><br>
<button>分享</button></form>
<h3>最近分享</h3>
<table><tr><th>仓库</th><th>任务</th><th>底座</th><th>状态</th></tr>
{% for i in items %}<tr><td><a href="/m/{{ i.id }}">{{ i.repo }}</a> <span class="mut">@{{ i.sha[:7] }}</span></td>
<td>{{ i.f.task or '—' }}</td><td>{{ i.f.base_model or '—' }}</td>
<td>{% if i.status == 'gone' %}<span class="warn">源文件已不可用</span>{% elif i.deprecated %}已弃用{% else %}—{% endif %}
{% if i.fl %} <span class="warn">内容提示 {{ i.fl|length }}</span>{% endif %}</td></tr>{% endfor %}
</table>"""

MODEL = """
<h2>{{ r.repo }} <span class="mut">@{{ r.sha[:7] }}</span></h2>
<p><a href="https://huggingface.co/{{ r.repo }}/tree/{{ r.sha }}" rel="nofollow noopener">在 Hugging Face 查看该提交</a>
 · 分享者 {{ r.owner }} · {{ r.shared_at }}
{% if r.status == 'gone' %} · <span class="warn">源文件已不可用</span>{% endif %}
{% if r.deprecated %} · 已弃用{% endif %}</p>
{% if flags %}<p class="warn">内容检查提示：卡片中出现 {{ flags|join('、') }} 类表述。平台不核实。</p>{% endif %}
{% if research.get('rank_ic') is not none or research.get('net_excess_ann') is not none %}
<section class="research-summary">
<div><span>样本外测试区间</span><strong>{{ research.test_period }}</strong><small>作者自报 · 未经平台验证</small></div>
{% if research.get('rank_ic') is not none %}<div><span>Rank IC</span><strong>{{ '%+.4f'|format(research.rank_ic) }}</strong><small>预测排序能力，不等同于收益</small></div>{% endif %}
{% if research.get('net_excess_ann') is not none %}<div><span>扣成本年化净超额</span><strong>{{ '%+.2f'|format(research.net_excess_ann) }}%</strong><small>基准与成本口径见作者说明</small></div>{% endif %}
</section>{% endif %}
<div class="cols">
<section class="box"><h3>工具生成的标准信息</h3>
{% if not f.qwb_version %}<p class="warn">不是 WiseHold 生成的卡片，以下字段未经工具产生。</p>{% endif %}
<table>{% for k, v in f.items() %}<tr><th>{{ k }}</th><td>{{ v }}</td></tr>{% endfor %}</table>
{% if missing %}<p class="mut">缺少字段：{{ missing|join(', ') }}</p>{% endif %}
</section>
<section class="box"><h3>作者自述 <span class="mut">（未经平台验证）</span></h3>
<pre>{{ r.author_text or '（无）' }}</pre></section></div>
{% if research.recipe %}<h3>训练配方 <span class="mut">· 作者提供，未经执行验证</span></h3><pre class="recipe-block">{{ research.recipe }}</pre>{% endif %}
<h3>版本</h3><table>{% for v in versions %}<tr><td><a href="/m/{{ v.id }}">@{{ v.sha[:7] }}</a></td>
<td>{{ v.shared_at }}</td><td>{% if v.deprecated %}已弃用{% endif %}{% if v.status=='gone' %} 源文件已不可用{% endif %}</td></tr>{% endfor %}</table>
{% if owner and not r.deprecated %}<form method="post" action="/m/{{ r.id }}/deprecate">
<input type="hidden" name="token" value="{{ csrf }}"><button>标记为已弃用（版本保留，不删除）</button></form>{% endif %}
"""

app = create_app() if os.environ.get("QWB_SITE_AUTOSTART", "1") == "1" else None
