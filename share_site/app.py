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

Run:  QWB_SITE_SECRET=... python -X utf8 -m uvicorn share_site.app:app --port 8800
"""

from __future__ import annotations

import json
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
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
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
    return con


def create_app(db_path: Path | None = None, hf: HFClient | None = None,
               oauth: dict | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    secret = secret or os.environ.get("QWB_SITE_SECRET") or secrets.token_urlsafe(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax",
                       https_only=bool(oauth))
    con = db(db_path or ROOT / "runs" / "site.db")
    hf = hf or HFClient(proxy=os.environ.get("HTTPS_PROXY"))
    oauth = oauth if oauth is not None else (
        {"client_id": os.environ["HF_OAUTH_CLIENT_ID"],
         "client_secret": os.environ["HF_OAUTH_CLIENT_SECRET"],
         "redirect_uri": os.environ["HF_OAUTH_REDIRECT_URI"]}
        if os.environ.get("HF_OAUTH_CLIENT_ID") else {})

    def user(req: Request) -> str | None:
        return req.session.get("user")

    def page(title: str, body: str, req: Request, **kw) -> HTMLResponse:
        t = env.from_string(LAYOUT)
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
    @app.get("/login")
    def login(req: Request):
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
        req.session.clear()
        req.session["user"] = str(name)[:96]          # the only personal datum stored
        return RedirectResponse("/", 302)

    @app.get("/login/dev")
    def login_dev(req: Request):
        if oauth or (req.client and req.client.host not in ("127.0.0.1", "::1", "testclient")):
            raise HTTPException(404)
        req.session["user"] = "local-dev"
        return RedirectResponse("/", 302)

    @app.get("/logout")
    def logout(req: Request):
        req.session.clear()
        return RedirectResponse("/", 302)

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def index(req: Request):
        rows = con.execute("select id, owner, repo, sha, fields, status, deprecated, flags "
                           "from shares order by id desc limit 200").fetchall()
        items = [dict(r) | {"f": json.loads(r["fields"]), "fl": json.loads(r["flags"])}
                 for r in rows]
        return page("模型分享", INDEX, req, items=items, csrf=csrf(req))

    @app.post("/share")
    def share(req: Request, repo: str = Form(...), author_text: str = Form(""),
              token: str = Form("")):
        u = user(req)
        if not u:
            raise HTTPException(401, "login first")
        check_csrf(req, token)
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
            " shared_at, checked_at) values (?,?,?,?,?,?,?,?,?)",
            (u, rid, sha, json.dumps(p["fields"], ensure_ascii=False),
             json.dumps(p["missing"]), json.dumps(p["flags"], ensure_ascii=False),
             author_text[:5000], _now(), _now()))
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
<header><strong><a href="/">qwb 模型分享</a></strong><span>
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
<div class="cols">
<section class="box"><h3>工具生成的标准信息</h3>
{% if not f.qwb_version %}<p class="warn">不是 qwb 生成的卡片，以下字段未经工具产生。</p>{% endif %}
<table>{% for k, v in f.items() %}<tr><th>{{ k }}</th><td>{{ v }}</td></tr>{% endfor %}</table>
{% if missing %}<p class="mut">缺少字段：{{ missing|join(', ') }}</p>{% endif %}
</section>
<section class="box"><h3>作者自述 <span class="mut">（未经平台验证）</span></h3>
<pre>{{ r.author_text or '（无）' }}</pre></section></div>
<h3>版本</h3><table>{% for v in versions %}<tr><td><a href="/m/{{ v.id }}">@{{ v.sha[:7] }}</a></td>
<td>{{ v.shared_at }}</td><td>{% if v.deprecated %}已弃用{% endif %}{% if v.status=='gone' %} 源文件已不可用{% endif %}</td></tr>{% endfor %}</table>
{% if owner and not r.deprecated %}<form method="post" action="/m/{{ r.id }}/deprecate">
<input type="hidden" name="token" value="{{ csrf }}"><button>标记为已弃用（版本保留，不删除）</button></form>{% endif %}
"""

app = create_app() if os.environ.get("QWB_SITE_AUTOSTART", "1") == "1" else None
