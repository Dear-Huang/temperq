"""Local web UI: fill in a form, run, read the report (SPEC §2 Windows one-click).

The UI only writes a run config (the same YAML `qwb run` takes) and submits it
to the local job service; it holds no training logic of its own. It listens on
127.0.0.1 only. POSTs need a per-process token and a localhost Host header, so a
web page open in the same browser cannot start or cancel runs.

    qwb ui            # opens http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import re
import secrets
import threading
from pathlib import Path
from typing import Callable

import yaml
# module level: with postponed annotations FastAPI must be able to resolve `Request`
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import daily, runconfig
from .jobs import JobSpec, Store

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SMOKE_TRAIN = {"universe": "all", "min_age_days": 20, "first_test_year": 2020,
               "final_test_years": [2021]}


def task_cards(task_dir: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(task_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(d, dict) and "template" in d and "label" in d:
            out[p.stem] = p
    return out


def build_config(form: dict, tasks: dict[str, Path]) -> tuple[dict | None, str]:
    """Form fields -> run config. Only values that differ from the defaults are written."""
    name = form.get("name", "").strip()
    if not NAME_RE.match(name):
        return None, "名称只能用字母、数字、下划线、连字符，最多 40 个字符"
    task = form.get("task", "")
    if task not in tasks:
        return None, "请选择一个任务"
    D = daily.DEFAULTS
    try:
        rounds = int(form.get("num_boost_round") or D["num_boost_round"])
        leaves = int(form.get("num_leaves") or D["lgbm"]["num_leaves"])
        lr = float(form.get("learning_rate") or D["lgbm"]["learning_rate"])
        n_seeds = int(form.get("n_seeds") or len(D["seeds"]))
        top = float(form.get("top") or D["portfolio"]["top"])
        comm = float(form.get("commission_bp") or D["portfolio"]["commission_bp"])
    except ValueError:
        return None, "数值格式不对"
    if not (1 <= n_seeds <= 10 and 10 <= rounds <= 5000 and 2 <= leaves <= 1024):
        return None, "种子数 1–10，树数 10–5000，叶子数 2–1024"
    smoke = form.get("source") == "synthetic"
    train = dict(SMOKE_TRAIN) if smoke else {}
    if not smoke and form.get("universe", D["universe"]) != D["universe"]:
        train["universe"] = form["universe"]
    if n_seeds != len(D["seeds"]):
        train["seeds"] = list(range(n_seeds))
    pf = {}
    if top != D["portfolio"]["top"]:
        pf["top"] = top
    if comm != D["portfolio"]["commission_bp"]:
        pf["commission_bp"] = comm
    if pf:
        train["portfolio"] = pf
    model: dict = {"name": "lightgbm"}
    if rounds != D["num_boost_round"]:
        model["num_boost_round"] = rounds
    params = {}
    if leaves != D["lgbm"]["num_leaves"]:
        params["num_leaves"] = leaves
    if lr != D["lgbm"]["learning_rate"]:
        params["learning_rate"] = lr
    if params:
        model["params"] = params
    cfg = {"name": name, "task": str(tasks[task]),
           "data": {"source": "synthetic" if smoke else "snapshot"},
           "model": model, "output": f"runs/{name}"}
    if train:
        cfg["train"] = train
    return cfg, ""


def create_app(workdir: Path, task_dir: Path | None = None,
               runner: Callable[[Callable[[], None]], None] | None = None, port: int = 8765):
    workdir = Path(workdir).resolve()
    task_dir = Path(task_dir or workdir / "examples")
    store = Store(workdir / "runs" / "jobs")
    cfg_dir = workdir / "runs" / "ui_configs"
    token = secrets.token_urlsafe(16)
    lock = threading.Lock()           # one training run at a time on this machine

    def default_runner(fn):
        threading.Thread(target=fn, daemon=True).start()

    runner = runner or default_runner
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}

    def guard(req: Request, tok: str) -> None:
        if req.headers.get("host") not in allowed_hosts:
            raise HTTPException(403, "bad host")
        if not secrets.compare_digest(tok or "", token):
            raise HTTPException(403, "bad token")

    def job_fn(rc: dict):
        from . import pipeline

        def fn(ctx, _cfg):
            def log(msg):
                ctx.log(str(msg))
                ctx.check_cancel()            # cancel takes effect between folds
            s = pipeline.run(rc, workdir, log=log)
            (ctx.artifacts / "output.txt").write_text(s["output"], encoding="utf-8")
        return fn

    def page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(LAYOUT.replace("{{title}}", html.escape(title))
                            .replace("{{body}}", body))

    @app.get("/", response_class=HTMLResponse)
    def index():
        tasks = task_cards(task_dir)
        D = daily.DEFAULTS
        opts = "".join(f'<option value="{html.escape(k)}">{html.escape(k)}</option>' for k in tasks)
        rows = []
        for j in reversed(store.list()):
            try:
                spec = store.spec(j["job_id"])
            except (OSError, ValueError):
                continue
            if spec.get("kind") != "train" or "ui" not in spec.get("config", {}):
                continue
            nm = spec["config"]["ui"]["name"]
            rows.append(f'<tr><td><a href="/job/{j["job_id"]}">{html.escape(nm)}</a></td>'
                        f'<td>{STATE.get(j["state"], j["state"])}</td><td>{j["updated"][:16]}</td></tr>')
        body = FORM.format(
            token=token, tasks=opts or '<option value="">（examples/ 下没有任务卡）</option>',
            rounds=D["num_boost_round"], leaves=D["lgbm"]["num_leaves"],
            lr=D["lgbm"]["learning_rate"], seeds=len(D["seeds"]),
            top=D["portfolio"]["top"], comm=D["portfolio"]["commission_bp"],
            rows="".join(rows) or '<tr><td colspan="3" class="mut">还没有运行</td></tr>')
        return page("qwb 本地工作台", body)

    @app.post("/run")
    async def run(req: Request):
        form = dict((await req.form()).items())
        guard(req, form.get("token", ""))
        cfg, err = build_config(form, task_cards(task_dir))
        if cfg is None:
            return page("配置有误", f'<p class="warn">{html.escape(err)}</p><p><a href="/">返回</a></p>')
        rc, rep = runconfig.resolve(cfg, workdir)
        if rc is None:
            return page("配置有误", f'<pre class="warn">{html.escape(str(rep))}</pre>'
                                    '<p><a href="/">返回</a></p>')
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{rc['name']}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        jid = store.submit(JobSpec("train", {"ui": {"name": rc["name"], "config": str(cfg_path)},
                                             "config_hash": rc["config_hash"]}))
        fn = job_fn(rc)

        def work():
            with lock:
                store.run(jid, fn)
        runner(work)
        return RedirectResponse(f"/job/{jid}", 303)

    @app.get("/job/{jid}", response_class=HTMLResponse)
    def job(jid: str):
        if not re.fullmatch(r"[0-9a-f]{12}", jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        st = store.status(jid)
        spec = store.spec(jid)
        name = spec["config"]["ui"]["name"]
        logs = [e for e in store.logs(jid) if e["type"] in ("log", "error")][-15:]
        log_txt = "\n".join(f"{e['t'][11:19]} {e.get('msg', '')}" for e in logs)
        report = ""
        out_file = store.root / jid / "artifacts" / "output.txt"
        if st["state"] == "succeeded" and out_file.exists():
            rp = Path(out_file.read_text(encoding="utf-8")) / "report.md"
            if rp.exists():
                report = f"<h3>报告</h3><pre>{html.escape(rp.read_text(encoding='utf-8'))}</pre>" \
                         f'<p class="mut">文件：{html.escape(str(rp))}</p>'
        running = st["state"] in ("queued", "running")
        cancel = (f'<form method="post" action="/job/{jid}/cancel"><input type="hidden" '
                  f'name="token" value="{token}"><button>取消</button></form>') if running else ""
        err = f'<p class="warn">{html.escape(st["error"] or "")}</p>' if st.get("error") else ""
        body = (f'<p><a href="/">← 返回</a></p><h2>{html.escape(name)}</h2>'
                f'<p>状态：<b>{STATE.get(st["state"], st["state"])}</b>　'
                f'配置：<code>{html.escape(spec["config"]["ui"]["config"])}</code></p>{err}{cancel}'
                f'<h3>日志</h3><pre>{html.escape(log_txt) or "（暂无）"}</pre>{report}')
        if running:
            body += '<meta http-equiv="refresh" content="5">'
        return page(name, body)

    @app.post("/job/{jid}/cancel")
    async def cancel(req: Request, jid: str):
        form = dict((await req.form()).items())
        guard(req, form.get("token", ""))
        if not re.fullmatch(r"[0-9a-f]{12}", jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        store.cancel(jid)
        return RedirectResponse(f"/job/{jid}", 303)

    app.state.token = token
    app.state.store = store
    return app


def serve(workdir: Path, port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    app = create_app(workdir, port=port)
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


STATE = {"queued": "排队中", "running": "运行中", "succeeded": "完成", "failed": "失败",
         "cancelled": "已取消"}

LAYOUT = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--acc:#2f5d8a;--warn:#9a3b1c;--code:#f1f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#2e2e2b;--acc:#8fb3d9;--warn:#e08a6a;--code:#22221f}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;margin:0;padding:0 16px}
main{max-width:960px;margin:0 auto;padding-block:24px} a{color:var(--acc)}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 12px;padding:10px 14px}
label{display:inline-block;margin:4px 16px 4px 0} input,select,button{font:inherit;padding:5px 8px;max-width:100%}
input[type=number]{width:7em} table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}
pre{background:var(--code);padding:12px;border-radius:6px;overflow-x:auto;white-space:pre;font-size:13px}
.mut{color:var(--mut)} .warn{color:var(--warn)}
</style></head><body><main>
<p><strong>qwb 本地工作台</strong> <span class="mut">· 只在本机运行，数据不离开这台电脑</span></p>
{{body}}
</main></body></html>"""

FORM = """
<form method="post" action="/run">
<input type="hidden" name="token" value="{token}">
<fieldset><legend>运行</legend>
<label>名称 <input name="name" required pattern="[A-Za-z0-9_\\-]{{1,40}}" placeholder="my_first_model"></label>
<label>任务 <select name="task">{tasks}</select></label>
<label>数据 <select name="source"><option value="snapshot">本地数据快照</option>
<option value="synthetic">试运行（合成数据，几秒钟）</option></select></label>
<label>股票池 <select name="universe"><option value="all">全部</option>
<option value="all_ex_st">剔除 ST</option><option value="st">只看 ST</option></select></label>
</fieldset>
<fieldset><legend>模型 LightGBM（不改就用默认值，改过的会列在报告首页）</legend>
<label>树的数量 <input type="number" name="num_boost_round" value="{rounds}" min="10" max="5000"></label>
<label>叶子数 <input type="number" name="num_leaves" value="{leaves}" min="2" max="1024"></label>
<label>学习率 <input type="number" name="learning_rate" value="{lr}" step="0.005" min="0.001" max="1"></label>
<label>种子数 <input type="number" name="n_seeds" value="{seeds}" min="1" max="10"></label>
</fieldset>
<fieldset><legend>扣成本检验</legend>
<label>持有前 <input type="number" name="top" value="{top}" step="0.01" min="0.01" max="0.5"> 比例</label>
<label>佣金（bp/边） <input type="number" name="commission_bp" value="{comm}" step="0.5" min="0"></label>
</fieldset>
<button>开始</button>
</form>
<h3>运行记录</h3>
<table><tr><th>名称</th><th>状态</th><th>更新时间（UTC）</th></tr>{rows}</table>
<p class="mut">命令行等价操作：<code>qwb run runs/ui_configs/&lt;名称&gt;.yaml</code></p>
"""
