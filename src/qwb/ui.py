"""Local web UI: four steps -- what to build, data, settings (optional), train.

The UI writes the same run config `qwb run` takes and submits it to the local
job service; it has no training logic of its own. It listens on 127.0.0.1
only. Every POST needs a per-process token and a localhost Host header, so a
web page open in the same browser cannot start runs or upload files.

    qwb ui            # opens http://127.0.0.1:8765
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml
# module level: with postponed annotations FastAPI must be able to resolve `Request`
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import daily, runconfig
from .jobs import JobSpec, Store

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
JOB_RE = re.compile(r"^[0-9a-f]{12}$")
SYNTHETIC = "_trial"

# what the user can build; key = task card file stem under examples/
TASKS = {
    "cn_rank_5d": {
        "title": "全 A 股选股",
        "desc": "每天收盘后，预测未来 5 个交易日哪些股票会相对更强（开盘买、5 天后开盘卖），剔除 ST 股。",
        "universe": "all_ex_st"},
    "st_task": {
        "title": "ST 股专题",
        "desc": "只在当天处于 ST 状态的股票里做同样的排序。需要数据里有 ST 标记。",
        "universe": "st"},
}


# ----------------------------------------------------------------- hardware

def hardware() -> dict:
    """What this machine has, in plain terms. Training never needs the GPU."""
    from . import env
    try:
        g = env.gpu()
    except Exception:
        g = None
    gpus, cpu = [], platform.processor() or platform.machine()
    if platform.system() == "Windows":
        try:
            import subprocess
            q = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_Processor).Name; '---'; "
                                "(Get-CimInstance Win32_VideoController).Name"],
                               capture_output=True, text=True, timeout=15)
            head, _, rest = q.stdout.partition("---")
            cpu = head.strip().splitlines()[0].strip() if head.strip() else cpu
            gpus = [x.strip() for x in rest.splitlines()
                    if x.strip() and "Idd" not in x and "Virtual" not in x]
        except Exception:
            pass
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        if g:
            gpus = [g["name"]]
    mem = None
    try:
        if platform.system() == "Windows":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong),
                            ("t", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("e", ctypes.c_ulonglong)]
            s = MS()
            s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            mem = s.t / 2 ** 30
        else:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "mem_gb": mem, "gpus": gpus}


# ----------------------------------------------------------------- datasets

def _read_snapshot(path: Path) -> dict | None:
    p = path / "snapshot.json"
    if not (p.is_file() and (path / "bars").is_dir()):
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def datasets(workdir: Path) -> dict[str, dict]:
    """id -> {path, snap, label}. Trial data is always first."""
    out = {SYNTHETIC: {"path": None, "label": "试运行数据（电脑自动生成，几秒钟跑完，只用来熟悉流程）",
                       "snap": {"start": "2019-01-01", "end": "2021-09-07", "has_st": False}}}
    paths: dict[str, Path] = {}
    root = workdir / "data"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                paths[d.name] = d
    reg = root / "registry.json"
    if reg.is_file():
        try:
            for k, v in json.loads(reg.read_text(encoding="utf-8")).items():
                paths.setdefault(k, Path(v))
        except (OSError, json.JSONDecodeError):
            pass
    from .pipeline import find_local_config
    local = find_local_config(workdir).get("data_dir")
    if local:
        paths.setdefault("local_snapshot", Path(local))
    for k, p in paths.items():
        s = _read_snapshot(p)
        if s is None:
            continue
        src = s.get("source", "tushare")
        out[k] = {"path": p, "snap": s, "label":
                  f"{k}：{s.get('start', '?')} 至 {s.get('end', '?')}，"
                  f"{s.get('trading_days', '?')} 个交易日"
                  + (f"，{s['codes']} 只股票" if "codes" in s else "")
                  + ("，含 ST 标记" if s.get("has_st", True) else "，无 ST 标记")
                  + f"（来源 {src.split(':')[0]}）"}
    return out


def register_folder(workdir: Path, path: str) -> str:
    p = Path(path.strip().strip('"')).expanduser()
    if _read_snapshot(p) is None:
        raise ValueError("这个文件夹里没有 snapshot.json 和 bars 目录，不是 qwb 数据文件夹")
    root = workdir / "data"
    root.mkdir(parents=True, exist_ok=True)
    reg = root / "registry.json"
    cur = json.loads(reg.read_text(encoding="utf-8")) if reg.is_file() else {}
    key = re.sub(r"[^A-Za-z0-9_-]", "_", p.name)[:30] or "folder"
    if key in cur and Path(cur[key]) != p:
        key = f"{key}_{hashlib.sha1(str(p).encode()).hexdigest()[:6]}"
    cur[key] = str(p)
    reg.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return key


def auto_years(snap: dict) -> tuple[int, list[int]]:
    """Test years from the data range: last two years (one if the data is short)."""
    y0, y1 = int(str(snap["start"])[:4]), int(str(snap["end"])[:4])
    final = [y1 - 1, y1] if y1 - y0 + 1 >= 5 else [y1]
    return max(y0 + 1, final[0] - 3), final


# ----------------------------------------------------------------- config

def build_config(form: dict, task_dir: Path, ds: dict[str, dict]) -> tuple[dict | None, str, list]:
    """Form -> run config. Only values that differ from the defaults are written."""
    notes = []
    name = form.get("name", "").strip()
    if not NAME_RE.match(name):
        return None, "名称只能用字母、数字、下划线、连字符，最多 40 个字符", notes
    task = form.get("task", "")
    if task not in TASKS or not (task_dir / f"{task}.yaml").is_file():
        return None, "第 1 步：请选择要打造的模型", notes
    dsid = form.get("dataset", "")
    if dsid not in ds:
        return None, "第 2 步：请选择数据", notes
    D = daily.DEFAULTS
    try:
        def num(k, default, cast):
            v = str(form.get(k, "")).strip()
            return cast(v) if v else default
        rounds = num("num_boost_round", D["num_boost_round"], int)
        leaves = num("num_leaves", D["lgbm"]["num_leaves"], int)
        lr = num("learning_rate", D["lgbm"]["learning_rate"], float)
        n_seeds = num("n_seeds", len(D["seeds"]), int)
        top = num("top", D["portfolio"]["top"], float)
        comm = num("commission_bp", D["portfolio"]["commission_bp"], float)
    except ValueError:
        return None, "第 3 步：有数值格式不对", notes
    if not (1 <= n_seeds <= 10 and 10 <= rounds <= 5000 and 2 <= leaves <= 1024
            and 0 < lr <= 1 and 0 < top <= 0.5 and 0 <= comm <= 100):
        return None, "第 3 步：种子数 1–10，树数 10–5000，叶子数 2–1024，学习率 0–1，持有比例 0–0.5", notes

    d = ds[dsid]
    universe = form.get("universe") or TASKS[task]["universe"]
    if universe != "all" and not d["snap"].get("has_st", True):
        if TASKS[task]["universe"] == "st":
            return None, "这份数据没有 ST 标记，不能做 ST 专题。请换数据，或在 CSV 里加 is_st 列", notes
        universe = "all"
        notes.append("这份数据没有 ST 标记，股票池改为全部股票（含 ST）")
    first, final = auto_years(d["snap"])
    train: dict = {}
    if universe != D["universe"]:
        train["universe"] = universe
    if first != D["first_test_year"]:
        train["first_test_year"] = first
    if final != D["final_test_years"]:
        train["final_test_years"] = final
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
    data = {"source": "synthetic"} if d["path"] is None else \
        {"source": "snapshot", "dir": str(d["path"])}
    cfg = {"name": name, "task": str(task_dir / f"{task}.yaml"), "data": data,
           "model": model, "output": f"runs/{name}"}
    if train:
        cfg["train"] = train
    return cfg, "", notes


# ----------------------------------------------------------------- report view

def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def md_to_html(md: str) -> str:
    """Enough Markdown for qwb's own reports: headings, lists, tables, bold, code."""
    out, table, lst = [], False, False

    def close():
        nonlocal table, lst
        if table:
            out.append("</table></div>")
        if lst:
            out.append("</ul>")
        table = lst = False
    for line in md.splitlines():
        t = line.strip()
        if t.startswith("|"):
            cells = [c.strip() for c in t.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not table:
                close()
                out.append('<div class="tw"><table>')
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
                table = True
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if t.startswith("#"):
            close()
            lvl = min(len(t) - len(t.lstrip("#")) + 1, 4)
            out.append(f"<h{lvl}>{_inline(t.lstrip('#').strip())}</h{lvl}>")
        elif t.startswith("- "):
            if not lst:
                close()
                out.append("<ul>")
                lst = True
            out.append(f"<li>{_inline(t[2:])}</li>")
        elif not t:
            close()
        else:
            close()
            out.append(f"<p>{_inline(t)}</p>")
    close()
    return "\n".join(out)


def verdict(m: dict) -> list[tuple[str, str]]:
    """Plain-language reading of a finished run: (level, sentence)."""
    out = []
    f = m["ic"]["lgbm_ensemble"]["final_test"]
    if f["n"] and f["t"] > 2 and f["mean"] > 0:
        out.append(("ok", f"有预测能力：测试期排序相关（IC）{f['mean']:+.3f}，t={f['t']:.1f}，统计上显著。"))
    else:
        out.append(("bad", f"预测能力不显著：测试期 IC {f['mean']:+.3f}，t={f['t']:.1f}。"))
    net = (m.get("net_of_cost") or {}).get("signals", {}).get("lgbm_ensemble")
    if net:
        nt = net["net_excess_t"]
        if net["net_excess_ann"] > 0 and nt > 2:
            out.append(("ok", f"扣掉交易成本后仍然显著赚钱：年化净超额 {net['net_excess_ann']:+.1%}（t={nt:.1f}）。"))
        elif net["net_excess_ann"] > 0:
            out.append(("warn", f"扣掉交易成本后还剩年化 {net['net_excess_ann']:+.1%}，但统计上不显著（t={nt:.1f}），"
                                f"不能确定是真本事还是运气。成本吃掉了 {net['cost_ann']:.1%}/年。"))
        else:
            out.append(("bad", f"扣掉交易成本后是亏的：年化净超额 {net['net_excess_ann']:+.1%}。"
                               f"成本 {net['cost_ann']:.1%}/年 超过了模型带来的收益。"))
    else:
        out.append(("warn", "没有做扣成本检验（缺 ashare-tcost），上面的 IC 不代表能赚钱。"))
    base = [k for k in m["ic"] if not k.startswith("lgbm")]
    best = max(base, key=lambda k: m["ic"][k]["final_test"]["mean"]) if base else None
    if best:
        b = m["ic"][best]["final_test"]["mean"]
        out.append(("ok" if f["mean"] > b else "bad",
                    f"{'比' if f['mean'] > b else '没有超过'}最好的简单方法（{best}，IC {b:+.3f}）"
                    + ("更好。" if f["mean"] > b else "，模型没有带来额外价值。")))
    lc, lg = m.get("leak_check"), m.get("ledger")
    if lc and not lc["ok"]:
        out.append(("bad", "前视检查没通过：特征可能用到了未来数据，结果不可信。"))
    if lg and lg["views"] > 1:
        out.append(("warn", f"这个测试期已经被评估过 {lg['views']} 次。反复调参再看同一段测试期，"
                            "结果会越来越好看但越来越不可信。"))
    if str(m.get("data_snapshot_id")) == "snap-synthetic":
        out.insert(0, ("warn", "这是试运行数据（电脑生成的），结果只说明流程走通了，没有任何投资含义。"))
    return out


# ----------------------------------------------------------------- app

def create_app(workdir: Path, task_dir: Path | None = None,
               runner: Callable[[Callable[[], None]], None] | None = None, port: int = 8765):
    workdir = Path(workdir).resolve()
    task_dir = Path(task_dir or workdir / "examples")
    store = Store(workdir / "runs" / "jobs")
    cfg_dir = workdir / "runs" / "ui_configs"
    data_root = workdir / "data"
    token = secrets.token_urlsafe(16)
    lock = threading.Lock()                   # one job at a time on this machine
    hw = hardware()

    def default_runner(fn):
        threading.Thread(target=fn, daemon=True).start()

    runner = runner or default_runner
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}

    def guard(req: Request, tok) -> None:
        if req.headers.get("host") not in allowed_hosts:
            raise HTTPException(403, "bad host")
        if not isinstance(tok, str) or not secrets.compare_digest(tok, token):
            raise HTTPException(403, "bad token")

    def submit(kind: str, cfg: dict, fn) -> str:
        jid = store.submit(JobSpec(kind, cfg))

        def work():
            with lock:
                store.run(jid, fn)
        runner(work)
        return jid

    def page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
        return HTMLResponse(LAYOUT.replace("{{title}}", html.escape(title))
                            .replace("{{refresh}}", '<meta http-equiv="refresh" content="4">'
                                     if refresh else "")
                            .replace("{{body}}", body))

    def oops(msg: str, back: str = "/") -> HTMLResponse:
        return page("出错了", f'<div class="box bad"><pre class="plain">{html.escape(msg)}</pre></div>'
                              f'<p><a class="btn" href="{back}">返回修改</a></p>')

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def index(ds: str = "", task: str = ""):
        avail = datasets(workdir)
        sel_ds = ds if ds in avail else (next((k for k in avail if k != SYNTHETIC), SYNTHETIC))
        sel_task = task if task in TASKS else "cn_rank_5d"
        tasks_html = "".join(
            f'<label class="card"><input type="radio" name="task" value="{k}"'
            f'{" checked" if k == sel_task else ""}><b>{html.escape(v["title"])}</b>'
            f'<span>{html.escape(v["desc"])}</span></label>'
            for k, v in TASKS.items() if (task_dir / f"{k}.yaml").is_file())
        ds_html = "".join(
            f'<label class="row"><input type="radio" name="dataset" value="{html.escape(k)}"'
            f'{" checked" if k == sel_ds else ""}> {html.escape(v["label"])}</label>'
            for k, v in avail.items())
        D = daily.DEFAULTS
        gpu = "、".join(hw["gpus"]) or "未检测到"
        mem = f"{hw['mem_gb']:.0f}GB" if hw["mem_gb"] else "未知"
        low_mem = hw["mem_gb"] is not None and hw["mem_gb"] < 8
        body = INDEX.format(
            token=token, tasks=tasks_html, datasets=ds_html,
            name=f"model_{datetime.now():%m%d_%H%M}",
            rounds=D["num_boost_round"], leaves=D["lgbm"]["num_leaves"],
            lr=D["lgbm"]["learning_rate"], seeds=len(D["seeds"]),
            top=D["portfolio"]["top"], comm=D["portfolio"]["commission_bp"],
            cpu=html.escape(f"{hw['cpu']}，{hw['cores']} 线程"), mem=mem, gpu=html.escape(gpu),
            memwarn='<p class="warn">内存不足 8GB，全市场数据可能跑不动，可以先用试运行或少量股票的 CSV。</p>'
            if low_mem else "",
            jobs=_job_rows(store))
        return page("qwb 本地工作台", body)

    @app.get("/template.csv")
    def template():
        return FileResponse(workdir / "examples" / "csv_template.csv",
                            filename="qwb_数据模板.csv", media_type="text/csv")

    @app.post("/run")
    async def run(req: Request):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        cfg, err, notes = build_config(form, task_dir, datasets(workdir))
        if cfg is None:
            return oops(err)
        rc, rep = runconfig.resolve(cfg, workdir)
        if rc is None:
            return oops(str(rep))
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{rc['name']}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")

        def fn(ctx, _cfg):
            from . import pipeline

            def log(msg):
                ctx.log(str(msg))
                ctx.check_cancel()            # cancel takes effect between folds
            for n in notes:
                ctx.log(n)
            s = pipeline.run(rc, workdir, log=log)
            (ctx.artifacts / "output.txt").write_text(s["output"], encoding="utf-8")
        jid = submit("train", {"ui": {"name": rc["name"], "config": str(cfg_path),
                                      "what": "train"}, "config_hash": rc["config_hash"]}, fn)
        return RedirectResponse(f"/job/{jid}", 303)

    @app.post("/data/csv")
    async def data_csv(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        up = form.get("file")
        name = str(form.get("name", "")).strip()
        if up is None or not getattr(up, "filename", ""):
            return oops("请选择一个 CSV 文件")
        from . import csvimport
        try:
            snap = csvimport.import_csv(up.file, data_root, name, up.filename)
        except csvimport.ImportError_ as e:
            return oops(str(e))
        except Exception as e:                # unexpected: show it, do not crash the page
            return oops(f"导入失败：{type(e).__name__}: {e}")
        notes = "".join(f"<li>{html.escape(n)}</li>" for n in snap["notes"])
        return page("导入成功", f'<div class="box ok"><b>导入成功：{html.escape(name)}</b>'
                                f'<p>{snap["start"]} 至 {snap["end"]}，{snap["trading_days"]} 个交易日，'
                                f'{snap["codes"]} 只股票，{snap["rows"]:,} 行。</p>'
                                + (f"<p>提示：</p><ul>{notes}</ul>" if notes else "") +
                                f'</div><p><a class="btn" href="/?ds={html.escape(name)}#step3">'
                                f'用这份数据继续 →</a></p>')

    @app.post("/data/folder")
    async def data_folder(req: Request):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        try:
            key = register_folder(workdir, str(form.get("path", "")))
        except (ValueError, OSError) as e:
            return oops(str(e))
        return RedirectResponse(f"/?ds={key}#step3", 303)

    @app.post("/data/tushare")
    async def data_tushare(req: Request):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        name = str(form.get("name", "")).strip()
        api_token = str(form.get("api_token", "")).strip()
        url = str(form.get("url", "")).strip() or "http://api.tushare.pro"
        if not NAME_RE.match(name):
            return oops("数据名称只能用字母、数字、下划线、连字符")
        if (data_root / name).exists():
            return oops(f"已经有叫 {name} 的数据了，换个名字")
        if len(api_token) < 16:
            return oops("请填写 Tushare token")
        if not re.match(r"^https?://[A-Za-z0-9.\-:/_]+$", url):
            return oops("接口地址格式不对")
        try:
            y0, y1 = int(form.get("start_year", 2018)), int(form.get("end_year", 2025))
        except ValueError:
            return oops("年份格式不对")
        if not (2005 <= y0 < y1 <= datetime.now().year) or y1 - y0 < 2:
            return oops("年份范围至少 3 年，起始年不早于 2005")
        from . import fetch
        job = fetch.make_job(api_token, url, data_root / "_cache", data_root / name)
        jid = submit("fetch", {"ui": {"name": name, "what": "fetch", "dataset": name},
                               "start": f"{y0}-01-01", "end": f"{y1}-12-31", "url": url}, job)
        return RedirectResponse(f"/job/{jid}", 303)

    @app.get("/job/{jid}", response_class=HTMLResponse)
    def job(jid: str):
        if not JOB_RE.match(jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        st, spec = store.status(jid), store.spec(jid)
        ui = spec["config"].get("ui", {})
        name, what = ui.get("name", jid), ui.get("what", spec["kind"])
        ev = store.logs(jid)
        logs = [e for e in ev if e["type"] in ("log", "error")][-12:]
        prog = [e for e in ev if e["type"] == "progress"]
        log_txt = "\n".join(f"{e['t'][11:19]}  {e.get('msg', '')}" for e in logs)
        running = st["state"] in ("queued", "running")
        head = {"queued": "排队中", "running": "正在运行…", "succeeded": "完成",
                "failed": "失败", "cancelled": "已取消"}[st["state"]]
        parts = [f'<p><a href="/">← 返回首页</a></p>',
                 f'<h2>{"训练" if what == "train" else "下载数据"}：{html.escape(name)}</h2>',
                 f'<p class="state {st["state"]}">{head}</p>']
        if prog:
            p = prog[-1]
            parts.append(f'<progress max="{p["total"]}" value="{p["done"]}"></progress> '
                         f'{p["done"]}/{p["total"]} {html.escape(p.get("msg", ""))}')
        if running and what == "train":
            parts.append('<p class="mut">全市场数据大约需要 5–20 分钟（取决于电脑），试运行几秒钟。'
                         '可以关掉这个页面，稍后在首页“运行记录”里打开。</p>')
        if running and what == "fetch":
            parts.append('<p class="mut">下载 8 年全市场数据大约 1 小时。中途断了可以重新提交同名下载，'
                         '已下载的部分会直接复用。</p>')
        if st.get("error"):
            parts.append(f'<div class="box bad"><b>出错信息</b><pre class="plain">'
                         f'{html.escape(st["error"])}</pre></div>')
        if running:
            parts.append(f'<form method="post" action="/job/{jid}/cancel"><input type="hidden" '
                         f'name="token" value="{token}"><button class="secondary">取消</button></form>')
        out_file = store.root / jid / "artifacts" / "output.txt"
        if st["state"] == "succeeded" and what == "train" and out_file.exists():
            out_dir = Path(out_file.read_text(encoding="utf-8"))
            m = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
            items = "".join(f'<li class="{lv}">{html.escape(s)}</li>' for lv, s in verdict(m))
            parts.append(f'<div class="box"><h3>一句话结论</h3><ul class="verdict">{items}</ul></div>')
            parts.append(f'<details open><summary>完整报告</summary>'
                         f'{md_to_html((out_dir / "report.md").read_text(encoding="utf-8"))}</details>')
            parts.append(f'<p class="mut">结果文件夹：<code>{html.escape(str(out_dir))}</code>（模型文件、'
                         '每日预测表 predictions.parquet、报告 report.md）</p>')
        if st["state"] == "succeeded" and what == "fetch":
            parts.append(f'<p><a class="btn" href="/?ds={html.escape(ui.get("dataset", ""))}#step3">'
                         '用这份数据继续 →</a></p>')
        parts.append(f'<details{" open" if st["state"] == "failed" else ""}><summary>运行日志</summary>'
                     f'<pre class="plain">{html.escape(log_txt) or "（暂无）"}</pre></details>')
        return page(name, "\n".join(parts), refresh=running)

    @app.post("/job/{jid}/cancel")
    async def cancel(req: Request, jid: str):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        if not JOB_RE.match(jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        store.cancel(jid)
        return RedirectResponse(f"/job/{jid}", 303)

    app.state.token = token
    app.state.store = store
    return app


def _job_rows(store: Store) -> str:
    rows = []
    label = {"queued": "排队中", "running": "运行中", "succeeded": "完成", "failed": "失败",
             "cancelled": "已取消"}
    for j in reversed(store.list()):
        try:
            spec = store.spec(j["job_id"])
        except (OSError, ValueError):
            continue
        ui = spec.get("config", {}).get("ui")
        if not ui:
            continue
        kind = "训练" if ui.get("what") == "train" else "下载数据"
        rows.append(f'<tr><td><a href="/job/{j["job_id"]}">{html.escape(ui["name"])}</a></td>'
                    f'<td>{kind}</td><td>{label.get(j["state"], j["state"])}</td>'
                    f'<td>{j["updated"][:16].replace("T", " ")}</td></tr>')
        if len(rows) >= 20:
            break
    return "".join(rows) or '<tr><td colspan="4" class="mut">还没有记录</td></tr>'


def free_port(start: int, tries: int = 20) -> int:
    """First port from `start` that 127.0.0.1 can bind (another program may hold 8765)."""
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise OSError(f"ports {start}-{start + tries - 1} are all in use")


def serve(workdir: Path, port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    port = free_port(port)
    app = create_app(workdir, port=port)
    if open_browser:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    print(f"qwb 工作台已启动：http://127.0.0.1:{port}  （关闭这个窗口即退出）", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


LAYOUT = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title>{{refresh}}
<style>
:root{--bg:#f6f5f1;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--acc:#2f5d8a;--accbg:#e9f0f7;
--ok:#2e6b3a;--okbg:#eaf4ec;--warn:#8a5a00;--warnbg:#fbf3e2;--bad:#9a3b1c;--badbg:#fbece6;--code:#f1f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#e8e7e2;--mut:#9a9993;--line:#34342f;
--acc:#8fb3d9;--accbg:#1f2a36;--ok:#8fc79a;--okbg:#1c2a1f;--warn:#e6c07a;--warnbg:#2c2618;--bad:#e8957a;--badbg:#2e1d17;--code:#262623}}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:0;padding:0 16px}
main{max-width:920px;margin:0 auto;padding-block:20px 60px}a{color:var(--acc)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:19px}h3{font-size:16px}
.step{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
.step>h2{margin:0 0 10px;font-size:17px}.num{display:inline-block;width:26px;height:26px;border-radius:50%;
background:var(--acc);color:var(--card);text-align:center;line-height:26px;font-size:14px;margin-right:8px}
.card{display:block;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0;cursor:pointer}
.card:has(input:checked){border-color:var(--acc);background:var(--accbg)}.card span{display:block;color:var(--mut);margin-left:22px}
.row{display:block;padding:6px 4px;cursor:pointer}
details{margin:10px 0}summary{cursor:pointer;color:var(--acc)}
.sub{border:1px dashed var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
label.f{display:inline-block;margin:6px 18px 6px 0}input,select,button{font:inherit;padding:6px 9px;max-width:100%;
border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
input[type=number]{width:7.5em}input[type=radio]{margin-right:6px}
button,.btn{background:var(--acc);color:var(--card);border:0;border-radius:8px;padding:9px 18px;cursor:pointer;
text-decoration:none;display:inline-block}button.big{font-size:17px;padding:12px 28px}
button.secondary{background:transparent;color:var(--acc);border:1px solid var(--acc)}
.mut{color:var(--mut)}.warn{color:var(--warn)}.hw{font-size:13px;color:var(--mut)}
.box{border-radius:10px;padding:12px 16px;margin:12px 0;background:var(--card);border:1px solid var(--line)}
.box.ok{background:var(--okbg)}.box.bad{background:var(--badbg)}
ul.verdict{padding-left:18px}ul.verdict li{margin:6px 0}li.ok{color:var(--ok)}li.warn{color:var(--warn)}li.bad{color:var(--bad)}
.state{font-size:18px;font-weight:600}.state.succeeded{color:var(--ok)}.state.failed{color:var(--bad)}
progress{width:260px;vertical-align:middle}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:6px 6px;text-align:left;vertical-align:top}
pre.plain{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:10px;border-radius:6px;font-size:13px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:13px}
</style></head><body><main>
<h1>qwb 本地量化工作台</h1>
<p class="mut">在自己电脑上训练选股模型。数据只在这台电脑上，不会上传。不构成投资建议。</p>
{{body}}
</main></body></html>"""

INDEX = """
<p class="hw">本机：{cpu}；内存 {mem}；显卡 {gpu}。<b>训练只用 CPU，什么显卡（含 AMD、核显、没有显卡）都不影响。</b></p>
{memwarn}
<form method="post" action="/run" id="main">
<input type="hidden" name="token" value="{token}">

<section class="step" id="step1"><h2><span class="num">1</span>要打造什么模型</h2>
{tasks}
</section>

<section class="step" id="step2"><h2><span class="num">2</span>用什么数据</h2>
{datasets}
<p class="mut">还没有数据？用下面任意一种方式添加，添加后会出现在上面的列表里。</p>
</section>
</form>

<section class="step" style="margin-top:-8px;border-top:0">
<details><summary>添加数据 · 方式 A：上传 CSV 文件</summary><div class="sub">
<p>每行是一只股票一天的数据。必填列：<code>code 代码</code> <code>date 日期</code> <code>open 开盘</code>
<code>high 最高</code> <code>low 最低</code> <code>close 收盘</code> <code>amount 成交额（元）</code>；
选填：<code>adj_factor 复权因子</code> <code>is_st 是否ST（0/1）</code>。至少 3 年。
<a href="/template.csv">下载模板</a></p>
<form method="post" action="/data/csv" enctype="multipart/form-data">
<input type="hidden" name="token" value="{token}">
<label class="f">数据名称 <input name="name" required pattern="[A-Za-z0-9_\\-]{{1,40}}" placeholder="my_data"></label>
<label class="f">文件 <input type="file" name="file" accept=".csv" required></label>
<button>上传并检查</button></form></div></details>

<details><summary>添加数据 · 方式 B：用 Tushare 账号自动下载（约 1 小时）</summary><div class="sub">
<p class="mut">需要能调用 daily、adj_factor、stk_limit、suspend_d、stock_st、trade_cal 这几个接口的 Tushare 账号或兼容服务（积分要求以 Tushare 官网为准）。token 只在本次下载中使用，不会保存到磁盘。</p>
<form method="post" action="/data/tushare">
<input type="hidden" name="token" value="{token}">
<label class="f">数据名称 <input name="name" required pattern="[A-Za-z0-9_\\-]{{1,40}}" value="cn_daily"></label>
<label class="f">Tushare token <input name="api_token" type="password" required size="30" autocomplete="off"></label><br>
<label class="f">接口地址 <input name="url" value="http://api.tushare.pro" size="30"></label>
<span class="mut">（用兼容服务时改成服务商给的地址）</span><br>
<label class="f">从 <input type="number" name="start_year" value="2018" min="2005"> 年</label>
<label class="f">到 <input type="number" name="end_year" value="2025"> 年</label>
<button>开始下载</button></form></div></details>

<details><summary>添加数据 · 方式 C：使用电脑上已有的 qwb 数据文件夹</summary><div class="sub">
<form method="post" action="/data/folder">
<input type="hidden" name="token" value="{token}">
<label class="f">文件夹路径 <input name="path" size="40" placeholder="例如 E:\\data\\qwb_data" required></label>
<button>添加</button></form></div></details>
</section>

<section class="step" id="step3"><h2><span class="num">3</span>参数（可以不改）</h2>
<label class="f">给这次训练起个名字 <input form="main" name="name" value="{name}" required
 pattern="[A-Za-z0-9_\\-]{{1,40}}"></label>
<details><summary>高级参数（不填就用默认值；改过的会写在报告首页）</summary><div class="sub">
<p><b>模型（LightGBM）</b></p>
<label class="f">树的数量 <input form="main" type="number" name="num_boost_round" placeholder="{rounds}" min="10" max="5000"></label>
<label class="f">叶子数 <input form="main" type="number" name="num_leaves" placeholder="{leaves}" min="2" max="1024"></label>
<label class="f">学习率 <input form="main" type="number" name="learning_rate" placeholder="{lr}" step="0.005" min="0.001" max="1"></label>
<label class="f">重复训练次数（种子） <input form="main" type="number" name="n_seeds" placeholder="{seeds}" min="1" max="10"></label>
<p><b>股票池</b></p>
<label class="f"><select form="main" name="universe"><option value="">按第 1 步的模型自动选</option>
<option value="all">全部股票（含 ST）</option><option value="all_ex_st">剔除 ST</option>
<option value="st">只要 ST</option></select></label>
<p><b>扣成本检验</b></p>
<label class="f">每次买入排名前 <input form="main" type="number" name="top" placeholder="{top}" step="0.01" min="0.01" max="0.5"> 的股票</label>
<label class="f">佣金（万分之几，单边） <input form="main" type="number" name="commission_bp" placeholder="{comm}" step="0.5" min="0"></label>
<p class="mut">测试年份按数据自动设定：最后两年作为最终测试期（数据少于 5 年时用最后一年）。</p>
</div></details>
</section>

<section class="step" id="step4"><h2><span class="num">4</span>开始</h2>
<button class="big" form="main">开始训练</button>
<span class="mut">　训练在本机进行，完成后自动给出报告和一句话结论。</span>
</section>

<h3>运行记录</h3>
<div class="tw"><table><tr><th>名称</th><th>类型</th><th>状态</th><th>更新时间（UTC）</th></tr>{jobs}</table></div>
"""
