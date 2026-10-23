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
from fastapi.staticfiles import StaticFiles

from . import daily, dataset, experiments, runconfig, scenario
from .jobs import JobSpec, Store

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
JOB_RE = re.compile(r"^[0-9a-f]{12}$")
SYNTHETIC = "_trial"

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
    if not dataset.is_dataset(path):
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
        if (p / "current.json").is_file():
            from .daily_data import current
            try:
                p = current(p)
            except (ValueError, OSError):
                continue
        s = _read_snapshot(p)
        if s is None:
            continue
        src = s.get("source", "tushare")
        n_src = len(dataset.spec(p)["sources"])
        out[k] = {"path": p, "snap": s, "has_index": dataset.has_index(p), "sources": n_src,
                  "combined": s.get("source") == "combined",
                  "label":
                  f"{k}：{s.get('start', '?')} 至 {s.get('end', '?')}，"
                  f"{s.get('trading_days', '?')} 个交易日"
                  + (f"，{s['codes']} 只股票" if s.get("codes") is not None else "")
                  + ("，含 ST 标记" if s.get("has_st", True) else "，无 ST 标记")
                  + ("，含指数成分" if dataset.has_index(p) else "")
                  + (f"，另加 {n_src} 个数据来源" if n_src else "")
                  + f"（来源 {src.split(':')[0]}）"}
    return out


def register_folder(workdir: Path, path: str) -> str:
    p = Path(path.strip().strip('"')).expanduser()
    if _read_snapshot(p) is None:
        raise ValueError("这个文件夹里没有 snapshot.json 和 bars 目录，不是 WiseHold 数据文件夹")
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

def parse_codes(text: str) -> list[str]:
    """Stock codes from free text: 000001, 600000.SH, separated by spaces, commas or lines."""
    from .csvimport import _suffix
    out = set()
    for tok in re.split(r"[\s,，;；、]+", text.strip().upper()):
        if re.fullmatch(r"\d{6}(\.(SH|SZ|BJ))?", tok):
            out.add(_suffix(tok))
    return sorted(out)


def build_config(form: dict, ds: dict[str, dict]) -> tuple[dict | None, str, list]:
    """Form -> run config. Step 1 (scenario) sets the task, universe and holdings;
    step 3 only overrides. Only values that differ from the defaults are written."""
    notes = []
    name = form.get("name", "").strip()
    if not NAME_RE.match(name):
        return None, "名称只能用字母、数字、下划线、连字符，最多 40 个字符", notes
    if form.get("market", "cn_a") != "cn_a":
        return None, "第 1 步：目前只支持 A 股", notes
    try:
        horizon = int(form.get("horizon") or scenario.DEFAULT_HORIZON)
    except ValueError:
        horizon = -1
    if horizon not in scenario.HORIZONS:
        return None, "第 1 步：请选择持有期", notes
    universe = form.get("universe") or "all_ex_st"
    if universe not in scenario.UNIVERSES:
        return None, "第 1 步：请选择股票池", notes
    tier = form.get("capital") or scenario.DEFAULT_TIER
    if tier not in scenario.TIERS:
        return None, "第 1 步：请选择资金量", notes
    model = form.get("model") or scenario.DEFAULT_MODEL
    if scenario.MODELS.get(model, {}).get("status") != "ready":
        return None, "第 1 步：这个模型还没有接入，请选 LightGBM 或 Ridge", notes
    dsid = form.get("dataset", "")
    if dsid not in ds:
        return None, "第 2 步：请选择数据", notes
    d = ds[dsid]
    if universe in ("st", "all_ex_st") and not d["snap"].get("has_st", True):
        if universe == "st":
            return None, "这份数据没有 ST 标记，不能只做 ST 股。请换数据，或在 CSV 里加 is_st 列", notes
        universe = "all"
        notes.append("这份数据没有 ST 标记，股票池改为全 A 股（含 ST）")
    if universe in scenario.INDEX_UNIVERSES and not d.get("has_index"):
        return None, (f"这份数据没有指数成分股历史，不能选“{scenario.UNIVERSES[universe]}”。"
                      "用 Tushare 下载的数据会自动带上；或改选全 A 股"), notes
    codes = None
    if universe == "list":
        codes = parse_codes(str(form.get("universe_list", "")))
        if len(codes) < 30:
            return None, f"自选股票名单至少 30 只（现在识别出 {len(codes)} 只），太少无法做截面排序", notes

    D = daily.DEFAULTS
    der = scenario.derive(tier)
    try:
        def num(k, default, cast):
            v = str(form.get(k, "")).strip()
            return cast(v) if v else default
        rounds = num("num_boost_round", D["num_boost_round"], int)
        leaves = num("num_leaves", D["lgbm"]["num_leaves"], int)
        lr = num("learning_rate", D["lgbm"]["learning_rate"], float)
        n_seeds = num("n_seeds", len(D["seeds"]), int)
        alpha = num("ridge_alpha", D["ridge_alpha"], float)
        n_hold = num("n_hold", der["n_hold"], int)
        comm = num("commission_bp", D["portfolio"]["commission_bp"], float)
    except ValueError:
        return None, "第 3 步：有数值格式不对", notes
    if not (1 <= n_seeds <= 10 and 10 <= rounds <= 5000 and 2 <= leaves <= 1024
            and 0 < lr <= 1 and 0 < alpha <= 1e6 and 1 <= n_hold <= 500 and 0 <= comm <= 100):
        return None, ("第 3 步：种子数 1–10，树数 10–5000，叶子数 2–1024，学习率 0–1，"
                      "持股只数 1–500，佣金 0–100"), notes
    min_adv = der["capital"] / n_hold / scenario.PARTICIPATION

    first, final = auto_years(d["snap"])
    train: dict = {}
    if universe != D["universe"]:
        train["universe"] = universe
    if codes:
        train["universe_list"] = codes
    if first != D["first_test_year"]:
        train["first_test_year"] = first
    if final != D["final_test_years"]:
        train["final_test_years"] = final
    if model == "ridge":
        train["seeds"] = [0]                     # deterministic: one fit is enough
    elif n_seeds != len(D["seeds"]):
        train["seeds"] = list(range(n_seeds))
    train["min_adv_yuan"] = round(min_adv)
    pf: dict = {"n_hold": n_hold}
    if comm != D["portfolio"]["commission_bp"]:
        pf["commission_bp"] = comm
    train["portfolio"] = pf
    train["scenario"] = {
        "market": "A 股", "horizon": scenario.HORIZONS[horizon],
        "universe": scenario.UNIVERSES[universe] + (f"（{len(codes)} 只）" if codes else ""),
        "capital": der["label"], "model": scenario.MODELS[model]["label"]}
    m: dict = {"name": model}
    if model == "lightgbm":
        if rounds != D["num_boost_round"]:
            m["num_boost_round"] = rounds
        params = {}
        if leaves != D["lgbm"]["num_leaves"]:
            params["num_leaves"] = leaves
        if lr != D["lgbm"]["learning_rate"]:
            params["learning_rate"] = lr
        if params:
            m["params"] = params
    elif alpha != D["ridge_alpha"]:
        m["alpha"] = alpha
    data = {"source": "synthetic"} if d["path"] is None else \
        {"source": "snapshot", "dir": str(d["path"])}
    cfg = {"name": name, "task": scenario.task_card(horizon), "data": data,
           "model": m, "train": train, "output": f"runs/{name}"}
    return cfg, "", notes


# ----------------------------------------------------------------- report view

def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def md_to_html(md: str) -> str:
    """Enough Markdown for qwb's own reports: headings, lists, tables, bold, code."""
    # Display legacy reports with the current brand without changing archived evidence.
    md = md.replace("Quant Workbench", "WiseHold").replace("QW-Bench", "WiseHold Bench")
    md = md.replace("qwb 可成交口径", "WiseHold 可成交口径").replace("qwb 的可成交口径", "WiseHold 的可成交口径").replace("qwb 口径", "WiseHold 口径")
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
    mk = m.get("model_key", "lgbm_ensemble")
    f = m["ic"][mk]["final_test"]
    if f["n"] and f["t"] > 2 and f["mean"] > 0:
        out.append(("ok", f"有预测能力：测试期排序相关（IC）{f['mean']:+.3f}，t={f['t']:.1f}，统计上显著。"))
    else:
        out.append(("bad", f"预测能力不显著：测试期 IC {f['mean']:+.3f}，t={f['t']:.1f}。"))
    net = (m.get("net_of_cost") or {}).get("signals", {}).get(mk)
    if net:
        nt = net["net_excess_t"]
        if net["net_excess_ann"] > 0 and nt > 2:
            out.append(("ok", f"当前假设下，历史年化净超额为 {net['net_excess_ann']:+.1%}（t={nt:.1f}）；未作实盘认证。"))
        elif net["net_excess_ann"] > 0:
            out.append(("warn", f"扣掉交易成本后还剩年化 {net['net_excess_ann']:+.1%}，但统计上不显著（t={nt:.1f}），"
                                f"不能确定是真本事还是运气。成本吃掉了 {net['cost_ann']:.1%}/年。"))
        else:
            out.append(("bad", f"扣掉交易成本后是亏的：年化净超额 {net['net_excess_ann']:+.1%}。"
                               f"成本 {net['cost_ann']:.1%}/年 超过了模型带来的收益。"))
    else:
        out.append(("warn", "没有做扣成本检验（缺 ashare-tcost），上面的 IC 不代表能赚钱。"))
    base = [k for k in m["ic"] if not k.startswith("lgbm") and k != mk]
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


def _radio(name, key, label, checked=False, disabled=False, note=""):
    tag = '<span class="tag">规划中</span>' if disabled else ""
    small = f"<small>{html.escape(note)}</small>" if note else ""
    return (f'<label class="opt{" off" if disabled else ""}"><input type="radio" name="{name}" '
            f'value="{key}" form="main"{" checked" if checked else ""}{" disabled" if disabled else ""}>'
            f'{html.escape(label)}{tag}{small}</label>')


def step1_html() -> str:
    mk = [_radio("market", "cn_a", "A 股", True)] + \
         [_radio("market", k, v, disabled=True) for k, v in scenario.PLANNED["market"]]
    hz = [_radio("horizon", str(h), v, h == scenario.DEFAULT_HORIZON,
                 note="样本少，过拟合风险高" if h == 20 else "")
          for h, v in scenario.HORIZONS.items()] + \
         [_radio("horizon", k, v, disabled=True) for k, v in scenario.PLANNED["horizon"]]
    uv = "".join(f'<option value="{k}"{" selected" if k == "all_ex_st" else ""}>{html.escape(v)}</option>'
                 for k, v in scenario.UNIVERSES.items())
    cap = "".join(
        f'<option value="{k}"{" selected" if k == scenario.DEFAULT_TIER else ""}>'
        f'{html.escape(v["label"])}</option>' for k, v in scenario.TIERS.items())
    dr = [_radio("direction", "long", "只做多", True)] + \
         [_radio("direction", k, v, disabled=True) for k, v in scenario.PLANNED["direction"]]
    cards, planned_cards = [], []
    for k, v in scenario.MODELS.items():
        off = v["status"] != "ready"
        (planned_cards if off else cards).append(
            f'<label class="card{" off" if off else ""}"><input type="radio" name="model" '
            f'value="{k}" form="main"{" checked" if k == scenario.DEFAULT_MODEL else ""}'
            f'{" disabled" if off else ""}><b>{html.escape(v["label"])}</b>'
            f'{"<span class=tag>规划中</span>" if off else ""}'
            f'<span>{html.escape(v["note"])}</span></label>')
    return (
        '<div class="scenario-grid">'
        f'<div class="q"><b>用在哪个市场</b><div>{"".join(mk)}</div></div>'
        f'<div class="q"><b>多久调一次仓（持有期）</b><div>{"".join(hz)}</div>'
        '<small class="mut">每天收盘后选股，第二天开盘买入，持有到期后开盘卖出。</small></div>'
        f'<div class="q"><b>在哪些股票里选</b><div><select name="universe" form="main" id="universe">{uv}</select></div>'
        '<div id="listbox" hidden><textarea name="universe_list" form="main" rows="3" '
        'placeholder="填股票代码，空格、逗号或换行分隔，例如 000001 600000.SH ……至少 30 只"></textarea></div>'
        '<small class="mut">指数成分股按每个历史时点当时的成分取，不用今天的名单回看过去。</small></div>'
        f'<div class="q"><b>打算投入多少资金</b><div><select name="capital" form="main" id="capital">{cap}</select></div>'
        '<small class="mut" id="capnote"></small></div>'
        f'<div class="q"><b>交易方式</b><div>{"".join(dr)}</div></div></div>'
        f'<div class="q"><b>用什么模型</b><div class="model-choices">{"".join(cards)}</div>'
        f'<details><summary>规划中的模型</summary>{"".join(planned_cards)}</details></div>')


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
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}

    def guard(req: Request, tok) -> None:
        if req.headers.get("host") not in allowed_hosts:
            raise HTTPException(403, "bad host")
        if not isinstance(tok, str) or not secrets.compare_digest(tok, token):
            raise HTTPException(403, "bad token")

    def submit(kind: str, cfg: dict, fn=None) -> str:
        jid = store.submit(JobSpec(kind, cfg))
        if cfg.get("action") in {"train", "protocol", "bench", "compare"}:
            from . import worker
            if runner is default_runner:
                try:
                    worker.launch(workdir, jid)
                except OSError as exc:
                    store._set(jid, "failed", str(exc))
                return jid
            fn = lambda ctx, payload: worker.execute(ctx, payload, workdir)

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
    def index(ds: str = ""):
        avail = datasets(workdir)
        sel_ds = ds if ds in avail else (next((k for k in avail if k != SYNTHETIC), SYNTHETIC))
        ds_html = "".join(
            f'<label class="row"><input type="radio" name="dataset" value="{html.escape(k)}"'
            f'{" checked" if k == sel_ds else ""}> {html.escape(v["label"])}'
            + ("" if k == SYNTHETIC else
               f' <a href="/data/{html.escape(k)}">{"管理数据来源" if v.get("combined") else "加更多数据来源"}</a>')
            + '</label>' for k, v in avail.items())
        D = daily.DEFAULTS
        gpu = "、".join(hw["gpus"]) or "未检测到"
        mem = f"{hw['mem_gb']:.0f}GB" if hw["mem_gb"] else "未知"
        low_mem = hw["mem_gb"] is not None and hw["mem_gb"] < 8
        tiers = {k: scenario.derive(k) for k in scenario.TIERS}
        script = SCRIPT.replace("__TIERS__", json.dumps(tiers, ensure_ascii=False)) \
                       .replace("__PART__", str(scenario.PARTICIPATION))
        body = INDEX.format(
            token=token, step1=step1_html(), datasets=ds_html,
            name=f"model_{datetime.now():%m%d_%H%M}",
            rounds=D["num_boost_round"], leaves=D["lgbm"]["num_leaves"],
            lr=D["lgbm"]["learning_rate"], seeds=len(D["seeds"]), alpha=D["ridge_alpha"],
            comm=D["portfolio"]["commission_bp"],
            cpu=html.escape(f"{hw['cpu']}，{hw['cores']} 线程"), mem=mem, gpu=html.escape(gpu),
            memwarn='<p class="warn">内存不足 8GB，全市场数据可能跑不动，可以先用试运行或少量股票的 CSV。</p>'
            if low_mem else "",
            jobs=_job_rows(store)) + script
        return page("训练模型", body)

    @app.get("/template.csv")
    def template():
        return FileResponse(workdir / "examples" / "csv_template.csv",
                            filename="wisehold_数据模板.csv", media_type="text/csv")

    @app.post("/run")
    async def run(req: Request):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        cfg, err, notes = build_config(form, datasets(workdir))
        if cfg is None:
            return oops(err)
        rc, rep = runconfig.resolve(cfg, workdir)
        if rc is None:
            return oops(str(rep))
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{rc['name']}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")

        jid = submit("train", {"action": "train", "resolved": rc, "notes": notes,
                              "ui": {"name": rc["name"], "config": str(cfg_path),
                                      "what": "train"}, "config_hash": rc["config_hash"]})
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

    @app.get("/data/{dsid}", response_class=HTMLResponse)
    def data_page(dsid: str):
        avail = datasets(workdir)
        if dsid not in avail or dsid == SYNTHETIC:
            raise HTTPException(404)
        d = avail[dsid]
        if not d.get("combined"):
            body = (f'<p><a href="/">← 返回首页</a></p><h2>给 {html.escape(dsid)} 加更多数据来源</h2>'
                    '<p>原数据保持不变。系统会新建一份“组合数据”：以它为基础，再叠加你后面添加的来源'
                    '（另一段行情、资金流、财务、自建因子、ST 标记……）。训练时选组合数据即可。</p>'
                    f'<form method="post" action="/data/{html.escape(dsid)}/combine">'
                    f'<input type="hidden" name="token" value="{token}">'
                    f'<label class="f">组合数据的名称 <input name="name" required value="{html.escape(dsid)}_plus"'
                    ' pattern="[A-Za-z0-9_\\-]{1,40}"></label><button>新建组合数据</button></form>')
            return page("加数据来源", body)
        sp = dataset.spec(d["path"])
        rows = []
        for s in sp["sources"]:
            what = dataset.KINDS[s["kind"]].split("（")[0]
            when = dataset.RULES.get(s.get("rule", ""), "") if s["kind"] == "factor" else \
                ("覆盖重叠部分" if s.get("priority") == "override" else "只补缺失部分") if s["kind"] == "bars" else ""
            cols = "、".join(s.get("columns", [])) or "—"
            notes = "；".join(s.get("notes", []))
            rows.append(
                f'<tr><td>{html.escape(s["label"])}<br><small class="mut">{html.escape(s["origin"])}</small></td>'
                f'<td>{html.escape(what)}</td><td>{html.escape(cols)}</td>'
                f'<td><small>{html.escape(when)}</small></td><td>{s["rows"]:,}'
                + (f'<br><small class="warn">{html.escape(notes)}</small>' if notes else "") +
                f'</td><td><form method="post" action="/data/{html.escape(dsid)}/remove">'
                f'<input type="hidden" name="token" value="{token}"><input type="hidden" name="sid" value="{s["id"]}">'
                '<button class="secondary">删除</button></form></td></tr>')
        base = sp["base"]
        kinds = "".join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in dataset.KINDS.items())
        rules = "".join(f'<label class="opt" style="display:block"><input type="radio" name="rule" value="{k}"'
                        f'{" checked" if k == "evening" else ""}>{html.escape(v)}</label>'
                        for k, v in dataset.RULES.items())
        apis = "".join(f'<label class="opt" style="display:block"><input type="checkbox" name="api" value="{k}">'
                       f'<b>{k}</b>　{html.escape(v)}</label>' for k, v in cn_a_factor_apis().items())
        table = ('<div class="tw"><table><tr><th>来源</th><th>是什么</th><th>指标列（会作为模型特征）</th>'
                 '<th>什么时候能用</th><th>行数</th><th></th></tr>' + "".join(rows) + '</table></div>') \
            if rows else '<p class="mut">还没有添加来源。</p>'
        body = (f'<p><a href="/?ds={html.escape(dsid)}#step2">← 返回首页（已选中这份数据）</a></p>'
                f'<h2>组合数据：{html.escape(dsid)}</h2>'
                f'<p class="mut">基础行情：{html.escape(str(base))}</p>{table}'
                '<div class="box"><h3>方式 1：上传 CSV</h3>'
                f'<form method="post" action="/data/{html.escape(dsid)}/source" enctype="multipart/form-data">'
                f'<input type="hidden" name="token" value="{token}">'
                f'<label class="f">这份数据是什么 <select name="kind" id="kind">{kinds}</select></label>'
                '<label class="f">给它起个名字 <input name="label" placeholder="例如：资金流、财务指标"></label><br>'
                '<label class="f">文件 <input type="file" name="file" accept=".csv" required></label>'
                '<div id="k-factor"><p><b>数据什么时候能拿到？</b>系统按这个时间把每一行放到它真正能用的那一天，'
                f'防止用到未来数据。</p>{rules}'
                '<p class="mut">格式：<code>code</code>、<code>date</code>（或 <code>ann_date</code> 公告日），'
                '其余每一列是一个数值指标，例如 <code>code,date,主力净流入,换手率</code>。</p></div>'
                '<div id="k-bars" hidden><p><b>和已有行情重叠时：</b></p>'
                '<label class="opt"><input type="radio" name="priority" value="fill" checked>只补已有数据缺的股票-日</label>'
                '<label class="opt"><input type="radio" name="priority" value="override">以新数据为准</label>'
                '<p class="mut">格式同 CSV 模板（code、date、open、high、low、close、amount）。重叠部分会先核对收盘价，'
                '口径不一致会拒绝导入。</p></div>'
                '<div id="k-st" hidden><p class="mut">格式：<code>code,date,is_st</code>（is_st 为 0/1）。会替换原有的 ST 标记。</p></div>'
                '<button>上传并检查</button></form></div>'
                '<div class="box"><h3>方式 2：用 Tushare 接口直接拉取</h3>'
                '<p class="mut">按基础数据的日期范围逐日拉取。token 只在本次使用，不保存。</p>'
                f'<form method="post" action="/data/{html.escape(dsid)}/tushare">'
                f'<input type="hidden" name="token" value="{token}">{apis}'
                '<label class="f">Tushare token <input name="api_token" type="password" required size="30" autocomplete="off"></label>'
                '<label class="f">接口地址 <input name="url" value="http://api.tushare.pro" size="30"></label>'
                '<button>开始拉取</button></form></div>'
                '<script>var k=document.getElementById("kind");function s(){["factor","bars","st"].forEach('
                'function(x){document.getElementById("k-"+x).hidden=k.value!==x;});}k.onchange=s;s();</script>')
        return page(f"数据来源：{dsid}", body)

    @app.post("/data/{dsid}/combine")
    async def data_combine(req: Request, dsid: str):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        avail = datasets(workdir)
        if dsid not in avail or dsid == SYNTHETIC:
            raise HTTPException(404)
        name = str(form.get("name", "")).strip()
        if not NAME_RE.match(name):
            return oops("名称只能用字母、数字、下划线、连字符")
        try:
            dataset.create(avail[dsid]["path"], data_root / name)
        except dataset.SourceError as e:
            return oops(str(e))
        return RedirectResponse(f"/data/{name}", 303)

    def _combined(dsid: str) -> Path:
        avail = datasets(workdir)
        if dsid not in avail or not avail[dsid].get("combined"):
            raise HTTPException(404)
        return avail[dsid]["path"]

    @app.post("/data/{dsid}/source")
    async def data_source(req: Request, dsid: str):
        form = await req.form()
        guard(req, form.get("token"))
        path = _combined(dsid)
        up = form.get("file")
        if up is None or not getattr(up, "filename", ""):
            return oops("请选择一个 CSV 文件", f"/data/{dsid}")
        try:
            src = dataset.add_csv(path, up.file.read(), str(form.get("kind", "")),
                                  str(form.get("rule", "")), str(form.get("label", "")).strip(),
                                  up.filename, str(form.get("priority", "fill")))
        except dataset.SourceError as e:
            return oops(str(e), f"/data/{dsid}")
        except Exception as e:
            return oops(f"导入失败：{type(e).__name__}: {e}", f"/data/{dsid}")
        return RedirectResponse(f"/data/{dsid}", 303)

    @app.post("/data/{dsid}/remove")
    async def data_remove(req: Request, dsid: str):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        path = _combined(dsid)
        sid = str(form.get("sid", ""))
        if not re.fullmatch(r"s(?:\d{1,4}|[0-9a-f]{16})", sid):
            raise HTTPException(400)
        dataset.remove(path, sid)
        return RedirectResponse(f"/data/{dsid}", 303)

    @app.post("/data/{dsid}/tushare")
    async def data_source_tushare(req: Request, dsid: str):
        form = await req.form()
        guard(req, form.get("token"))
        path = _combined(dsid)
        apis = [a for a in form.getlist("api") if a in cn_a_factor_apis()]
        api_token = str(form.get("api_token", "")).strip()
        url = str(form.get("url", "")).strip() or "http://api.tushare.pro"
        if not apis:
            return oops("请至少勾选一个接口", f"/data/{dsid}")
        if len(api_token) < 16:
            return oops("请填写 Tushare token", f"/data/{dsid}")
        if not re.match(r"^https?://[A-Za-z0-9.\-:/_]+$", url):
            return oops("接口地址格式不对", f"/data/{dsid}")
        from . import fetch
        job = fetch.make_factor_job(api_token, url, data_root / "_cache", path, apis)
        jid = submit("fetch", {"ui": {"name": f"{dsid}: {'、'.join(apis)}", "what": "fetch",
                                      "dataset": dsid}, "apis": apis, "url": url}, job)
        return RedirectResponse(f"/job/{jid}", 303)

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
        if st["state"] == "running":
            try:
                store.recover(jid)
            except ValueError:
                pass
            st = store.status(jid)
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
                 f'<h2>{ {"train": "训练", "audit": "体检", "bench": "Bench", "compare": "版本比较", "protocol": "冻结协议"}.get(what, "下载数据")}：{html.escape(name)}</h2>',
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
        if st["state"] in ("failed", "cancelled") and spec["config"].get("action"):
            parts.append(f'<form method="post" action="/job/{jid}/retry"><input type="hidden" '
                         f'name="token" value="{token}"><button>重新运行</button></form>')
        if running:
            parts.append(f'<form method="post" action="/job/{jid}/cancel"><input type="hidden" '
                         f'name="token" value="{token}"><button class="secondary">取消</button></form>')
        out_file = store.root / jid / "artifacts" / "output.txt"
        if what == "protocol" and st["state"] == "succeeded":
            parts.append(f'<p><a href="/experiments/{html.escape(ui.get("run_id", ""))}">返回训练版本</a></p>')
        if what in ("bench", "compare") and out_file.exists():
            out_dir = Path(out_file.read_text(encoding="utf-8"))
            link = f'/evaluations/{out_dir.name}' if what == "bench" else f'/comparisons/{out_dir.name}'
            parts.append(f'<p><a class="btn" href="{link}">查看评价结果</a></p>')
        if st["state"] == "succeeded" and what in ("train", "audit") and out_file.exists():
            out_dir = Path(out_file.read_text(encoding="utf-8"))
            m = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
            items = "".join(f'<li class="{lv}">{html.escape(s)}</li>' for lv, s in verdict(m))
            parts.append(f'<div class="box"><h3>一句话结论</h3><ul class="verdict">{items}</ul></div>')
            parts.append(f'<details open><summary>完整报告</summary>'
                         f'{md_to_html((out_dir / "report.md").read_text(encoding="utf-8"))}</details>')
            if (out_dir / "final_model" / "meta.json").is_file():
                parts.append(f'<p><a class="btn" href="/models/{html.escape(out_dir.name)}">'
                             '用这个模型出今天的持仓清单 →</a></p>')
            if (out_dir / "run.json").is_file():
                parts.append(f'<p><a href="/experiments/{out_dir.name}">训练版本与独立 Bench</a></p>')
            parts.append(f'<p class="mut">结果文件夹：<code>{html.escape(str(out_dir))}</code>（模型文件、'
                         '每日预测表 predictions.parquet、报告 report.md）</p>')
        if st["state"] == "succeeded" and what == "fetch":
            parts.append(f'<p><a class="btn" href="/?ds={html.escape(ui.get("dataset", ""))}#step3">'
                         '用这份数据继续 →</a></p>')
        parts.append(f'<details{" open" if st["state"] == "failed" else ""}><summary>运行日志</summary>'
                     f'<pre class="plain">{html.escape(log_txt) or "（暂无）"}</pre></details>')
        return page(name, "\n".join(parts), refresh=running)

    # ---------------------------------------------------------------- audit external models
    @app.get("/audit", response_class=HTMLResponse)
    def audit_page():
        avail = {k: v for k, v in datasets(workdir).items()}
        sel = next((k for k in avail if k != SYNTHETIC), SYNTHETIC)
        ds_opts = "".join(f'<option value="{html.escape(k)}"{" selected" if k == sel else ""}>'
                          f'{html.escape(v["label"])}</option>' for k, v in avail.items())
        hz = "".join(f'<option value="{h}"{" selected" if h == scenario.DEFAULT_HORIZON else ""}>'
                     f'{html.escape(v)}</option>' for h, v in scenario.HORIZONS.items())
        cap = "".join(f'<option value="{k}"{" selected" if k == scenario.DEFAULT_TIER else ""}>'
                      f'{html.escape(v["label"])}</option>' for k, v in scenario.TIERS.items())
        uv = "".join(f'<option value="{k}">{html.escape(v)}</option>'
                     for k, v in scenario.UNIVERSES.items() if k != "list")
        body = ('<h2>体检外部模型</h2>'
                '<p>模型在 Qlib、vn.py、自己的 Python 脚本里训练都可以。只要上传它每天给每只股票的打分，'
                'WiseHold 会用你的数据、按可成交的规则重新检验：扣成本后还剩多少、能不能赢简单方法、'
                '换成 Qlib 默认回测口径会被高估多少、打分有没有前视嫌疑。不需要模型文件。</p>'
                '<div class="box"><b>打分文件格式（CSV）</b><p>每行一只股票一天：'
                '<code>code</code> 股票代码、<code>date</code> 打分日期（当天收盘后算出的）、<code>score</code> 打分。'
                '至少覆盖 60 个交易日。CSV 或 Parquet 都可以。</p>'
                '<p><b>常见工具的导出方式</b></p><ul>'
                '<li><b>Qlib</b>：<code>pred.reset_index().to_csv("pred.csv", index=False)</code>，'
                '或直接存 <code>pred.to_parquet("pred.parquet")</code>（列 datetime、instrument、score）</li>'
                '<li><b>vn.py（vnpy.alpha）</b>：信号表导出 CSV，列 datetime、vt_symbol、signal</li>'
                '<li><b>聚宽 / 米筐</b>：代码 600000.XSHG、000001.XSHE 可直接用</li>'
                '<li><b>BaoStock、Tushare、Wind、自己的脚本</b>：sh.600000、600000.SH、600000 都能识别</li></ul>'
                '<p class="mut">pickle 文件（.pkl）不能直接上传：打开 pickle 可能执行其中的代码。</p></div>'
                f'<form method="post" action="/audit" enctype="multipart/form-data" class="box">'
                f'<input type="hidden" name="token" value="{token}">'
                '<label class="f">名称 <input name="name" required pattern="[A-Za-z0-9_\\-]{1,40}" '
                f'value="ext_{datetime.now():%m%d_%H%M}"></label>'
                '<label class="f">打分文件 <input type="file" name="file" accept=".csv,.parquet" required></label><br>'
                f'<label class="f">用哪份行情数据检验 <select name="dataset">{ds_opts}</select></label><br>'
                f'<label class="f">持有期 <select name="horizon">{hz}</select></label>'
                f'<label class="f">股票池 <select name="universe">{uv}</select></label>'
                f'<label class="f">资金量 <select name="capital">{cap}</select></label>'
                '<p class="mut">持有期要和模型训练时的预测目标一致，比如 Qlib 默认标签是 2 天收益，选“持有 1 天”最接近。</p>'
                '<button class="big">开始体检</button></form>')
        return page("体检外部模型", body)

    @app.post("/audit")
    async def audit_run(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        name = str(form.get("name", "")).strip()
        if not NAME_RE.match(name):
            return oops("名称只能用字母、数字、下划线、连字符", "/audit")
        up = form.get("file")
        if up is None or not getattr(up, "filename", ""):
            return oops("请选择打分文件", "/audit")
        raw = up.file.read()
        if len(raw) > 300 * 2 ** 20:
            return oops("文件超过 300MB，请只保留需要检验的日期", "/audit")
        avail = datasets(workdir)
        dsid = str(form.get("dataset", ""))
        if dsid not in avail:
            return oops("请选择行情数据", "/audit")
        try:
            horizon = int(form.get("horizon", scenario.DEFAULT_HORIZON))
        except ValueError:
            horizon = -1
        tier, universe = str(form.get("capital", "")), str(form.get("universe", "all"))
        if horizon not in scenario.HORIZONS or tier not in scenario.TIERS \
                or universe not in scenario.UNIVERSES or universe == "list":
            return oops("持有期、股票池或资金量不对", "/audit")
        from . import audit
        try:
            audit.read_predictions(raw)               # fail fast on format, before queueing
        except audit.AuditError as e:
            return oops(str(e), "/audit")
        d = avail[dsid]
        out = runs_root / name

        def fn(ctx, _cfg):
            s = audit.run(raw, up.filename, d["path"], horizon, tier, out, workdir, name,
                          universe=universe, log=ctx.log)
            (ctx.artifacts / "output.txt").write_text(str(out), encoding="utf-8")
        jid = submit("evaluate", {"ui": {"name": name, "what": "audit", "file": up.filename}}, fn)
        return RedirectResponse(f"/job/{jid}", 303)

    # ---------------------------------------------------------------- use a model
    runs_root = workdir / "runs"

    def trained_models() -> list[dict]:
        out = []
        if not runs_root.is_dir():
            return out
        for d in runs_root.iterdir():
            mp = d / "final_model" / "meta.json"
            if not mp.is_file():
                continue
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
                m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            net = (m.get("net_of_cost") or {}).get("signals", {}).get(m.get("model_key", "lgbm_ensemble"))
            f = m["ic"][m.get("model_key", "lgbm_ensemble")]["final_test"]
            out.append({"run": d.name, "meta": meta, "m": m, "net": net, "ic": f,
                        "mtime": mp.stat().st_mtime})
        return sorted(out, key=lambda x: -x["mtime"])

    def _run_dir(run: str) -> Path:
        if not NAME_RE.match(run):
            raise HTTPException(404)
        d = experiments.run_dir(workdir, run) if experiments.ID_RE.fullmatch(run) else runs_root / run
        if not (d / "final_model" / "meta.json").is_file():
            raise HTTPException(404)
        try:
            return experiments.frozen_source(d)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/models", response_class=HTMLResponse)
    def models_page():
        cards = []
        for x in trained_models():
            sc = x["m"].get("scenario") or {}
            net = x["net"]
            synth = x["meta"].get("snapshot_id") == "snap-synthetic"
            ok = (not synth) and x["m"].get("go") and net and net["net_excess_t"] > 2
            label, tone = ("试运行", "pending") if synth else ("历史诊断通过", "active") if ok else ("证据不足", "pending")
            name = html.escape(x["run"])
            net_txt = "仅演示" if synth else "未计算" if not net else f"{net['net_excess_ann']:+.1%}"
            note = "合成数据，无投资含义" if synth else "未通过检验" if not ok else "历史诊断，不代表实盘认证"
            net_t = "" if synth or not net else f't={net["net_excess_t"]:.2f}'
            cards.append(f'<article class="account-card model-result" data-model-card><div class="card-heading">'
                f'<span class="scope-label">{html.escape(x["meta"]["model"])}</span><span class="status-badge {tone}">{label}</span></div>'
                f'<h2><a href="/models/{name}">{name}</a></h2><p class="mut">'
                f'{html.escape(sc.get("universe") or x["m"].get("universe", ""))} · '
                f'{html.escape(sc.get("horizon", "5 日"))}</p><div class="metrics compact-metrics">'
                f'<div class="metric"><span>扣成本后年化净超额</span><strong>{net_txt}</strong><small>{net_t}</small></div>'
                f'<div class="metric"><span>测试期 IC</span><strong>{x["ic"]["mean"]:+.3f}</strong>'
                f'<small>t={x["ic"]["t"]:.2f}</small></div></div><p class="evidence-note">{note}</p>'
                f'<div class="card-meta"><span>数据截止 {html.escape(x["meta"].get("data_end", "未知"))}</span></div>'
                f'<a class="card-link" href="/models/{name}">查看模型与清单</a></article>')
        table = '<div class="account-grid">' + ''.join(cards) + '</div>' if cards else (
            '<div class="empty-state"><h3>还没有训练好的模型</h3><a class="btn" href="/">训练第一个模型</a></div>')
        body = ('<div class="page-heading"><div><h1>使用模型</h1>'
                f'<p class="mut">{len(cards)} 个模型 · 历史诊断与每日账户分别查看</p></div>'
                '<a class="btn" href="/">新建训练</a></div>'
                '<p class="notice">历史诊断和参考清单不代表可投入实盘；真实账户建议需绑定现金与持仓。</p>' + table)
        return page("使用模型", body)

    def _picks_or_error(run: str, date: str, capital: str):
        from . import picks
        cap = None
        if capital.strip():
            try:
                cap = float(capital) * 1e4
            except ValueError:
                raise ValueError("资金格式不对，填数字，单位万元") from None
            if not (0 < cap <= 1e11):
                raise ValueError("资金应在 0 到 1000 亿元之间")
        if date.strip() and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.strip()):
            raise ValueError("日期格式应为 2025-12-31")
        return picks.picks(_run_dir(run), workdir, date.strip() or None, cap)

    @app.get("/models/{run}", response_class=HTMLResponse)
    def model_page(run: str, date: str = "", capital: str = ""):
        d = _run_dir(run)
        meta = json.loads((d / "final_model" / "meta.json").read_text(encoding="utf-8"))
        m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        from . import picks as picks_mod
        if meta.get("data", {}).get("source") != "synthetic" and not picks_mod.is_ready(d):
            # full-market data takes about a minute to load; do it in the background
            picks_mod.warm(d, workdir)
            return page(f"使用模型：{run}",
                        f'<p><a href="/models">← 全部模型</a></p><h2>{html.escape(run)}</h2>'
                        '<div class="box"><b>正在载入数据……</b><p>第一次打开要把全部行情和特征读进内存，'
                        '全市场数据大约 1–2 分钟。页面会自动刷新；载入后再换日期、改资金、下载 CSV 都是即时的。</p></div>',
                        refresh=True)
        try:
            r = _picks_or_error(run, date, capital)
        except (ValueError, FileNotFoundError) as e:
            return oops(str(e), f"/models/{run}")
        sc = m.get("scenario") or {}
        warn = []
        if meta.get("snapshot_id") == "snap-synthetic":
            warn.append("这是试运行数据训练的模型，清单没有任何投资含义。")
        net = (m.get("net_of_cost") or {}).get("signals", {}).get(m.get("model_key", "lgbm_ensemble"))
        if not m.get("go") or not net or net["net_excess_t"] <= 2:
            warn.append("这个模型在测试期没有通过检验（扣成本后不显著，或没赢过简单方法）。清单只用来观察，不建议照着交易。")
        if r["stale_days"] > 5 and meta.get("snapshot_id") != "snap-synthetic":
            warn.append(f"数据最后一天是 {r['data_end']}，已经过去 {r['stale_days']} 天。请先更新该模型绑定的数据源；固定模型每日推理不需要重训。")
        if r["date"] != r["data_end"]:
            warn.append(f"现在看的是历史日期 {r['date']} 的清单，不是最新的。")
        acts = {"买入": "ok", "卖出": "bad", "继续持有": ""}
        rows = "".join(
            f'<tr><td>{x.rank if x.rank == x.rank else "—"}</td><td>{html.escape(x.code)}</td>'
            f'<td class="{acts.get(x.action, "")}"><b>{html.escape(x.action)}</b></td>'
            f'<td>{x.weight:.1%}</td><td>{"" if x.ref_close is None or x.ref_close != x.ref_close else f"{x.ref_close:.2f}"}</td>'
            f'<td>{"" if x.est_shares is None or x.est_shares != x.est_shares else f"{int(x.est_shares):,}"}</td>'
            f'<td><small class="warn">{html.escape(x.note or "")}</small></td></tr>'
            for x in r["table"].itertuples())
        n = r["table"]["action"].value_counts().to_dict()
        cap_txt = scenario.fmt_yuan(r["capital"]) if r["capital"] else "未设定"
        cap_ph = f"{r['capital'] / 1e4:g}" if r["capital"] else ""
        body = (f'<p class="breadcrumb"><a href="/models">全部模型</a> / 模型详情</p>'
                f'<div class="page-heading"><div><span class="scope-label">{html.escape(meta["model"])}</span><h1>{html.escape(run)}</h1></div>'
                f'<a class="btn" href="/models/{html.escape(run)}/account">账户交易建议</a></div>'
                f'<p class="mut">{html.escape(" · ".join(x for x in (sc.get("universe"), sc.get("horizon"), sc.get("model") or meta["model"]) if x))}'
                f' · 训练数据到 {html.escape(meta.get("train_end", ""))}</p>'
                + ('<aside class="notice warn"><ul>' + ''.join(f'<li>{html.escape(w)}</li>' for w in warn) + '</ul></aside>' if warn else '') +
                f'<form method="get" class="toolbar"><label class="f">清单日期 <input name="date" type="date" value="{html.escape(date or r["date"])}" '
                f'placeholder="{r["data_end"]}（最新）" size="14"></label>'
                f'<label class="f">资金（万元） <input name="capital" value="{html.escape(capital)}" '
                f'placeholder="{cap_ph}" size="10"></label>'
                '<button>更新清单</button></form>'
                f'<section class="data-section"><div class="section-heading"><h2>模型参考清单</h2><span class="mut">{r["date"]} 收盘后的决策，{r["trade_on"]}执行</span></div>'
                '<div class="metrics account-metrics">'
                f'<div class="metric"><span>目标持仓</span><strong>{r["n_hold"]} 只</strong></div>'
                f'<div class="metric"><span>参考资金</span><strong>{cap_txt}</strong></div>'
                f'<div class="metric"><span>买入 / 卖出</span><strong>{n.get("买入", 0)} / {n.get("卖出", 0)}</strong></div>'
                f'<div class="metric"><span>继续持有</span><strong>{n.get("继续持有", 0)} 只</strong></div></div>'
                '<details><summary>选股口径与参考价</summary>'
                f'<p>股票池今天有 {r["universe_size"]} 只可选，按模型打分取前 {r["n_hold"]} 只等权持有，'
                f'每只约 {cap_txt if not r["per_name"] else scenario.fmt_yuan(r["per_name"])}，持有 {r["hold_days"]} 个交易日后按同样方法换仓。'
                f'相对 {r["hold_days"]} 个交易日前的清单：买入 {n.get("买入", 0)} 只，卖出 {n.get("卖出", 0)} 只，'
                f'继续持有 {n.get("继续持有", 0)} 只。</p>'
                '<p class="mut">本页是模型参考清单，操作比较前后模型选股，不读取你的实际账户。'
                '参考价是当天收盘价；股数只作估算，科创板应按 200 股起、其后 1 股递增。'
                '实际成交价是明天开盘价，可能不同。</p></details></section>'
                f'<p><a class="btn" href="/models/{html.escape(run)}/picks.csv?date={html.escape(date)}&capital={html.escape(capital)}">'
                '下载持仓清单 CSV</a>　<a href="/models/' + html.escape(run) + f'/scores.csv?date={html.escape(date)}">'
                '下载全部股票打分 CSV</a></p>'
                '<div class="tw"><table class="holdings-table"><tr><th>排名</th><th>代码</th><th>操作</th><th>目标权重</th>'
                f'<th>参考价</th><th>估算股数</th><th>提示</th></tr>{rows}</table></div>'
                '<p class="mut">不构成投资建议。模型只根据历史量价规律排序，不知道公告、新闻和你的个人情况。</p>')
        return page(f"使用模型：{run}", body)

    @app.get("/models/{run}/picks.csv")
    def picks_csv(run: str, date: str = "", capital: str = ""):
        from fastapi.responses import Response
        try:
            r = _picks_or_error(run, date, capital)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from None
        tab = r["table"].rename(columns={"code": "代码", "rank": "排名", "score": "模型打分", "action": "操作",
                                         "weight": "目标权重", "ref_close": "参考价", "est_shares": "估算股数",
                                         "note": "提示"})
        data = tab.to_csv(index=False).encode("utf-8-sig")       # BOM so Excel shows Chinese
        return Response(data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{run}_{r["date"]}_holdings.csv"'})

    @app.get("/models/{run}/scores.csv")
    def scores_csv(run: str, date: str = ""):
        from fastapi.responses import Response
        try:
            r = _picks_or_error(run, date, "")
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from None
        data = r["full_rank"].rename(columns={"code": "代码", "rank": "排名", "score": "模型打分"}) \
            .to_csv(index=False).encode("utf-8-sig")
        return Response(data, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{run}_{r["date"]}_scores.csv"'})

    @app.post("/job/{jid}/cancel")
    async def cancel(req: Request, jid: str):
        form = dict((await req.form()).items())
        guard(req, form.get("token"))
        if not JOB_RE.match(jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        store.cancel(jid)
        return RedirectResponse(f"/job/{jid}", 303)

    @app.post("/job/{jid}/retry")
    async def retry(req: Request, jid: str):
        form = await req.form()
        guard(req, form.get("token"))
        if not JOB_RE.fullmatch(jid) or not (store.root / jid).is_dir():
            raise HTTPException(404)
        from . import worker
        if store.spec(jid)["config"].get("action") not in worker.ACTIONS:
            raise HTTPException(400, "此任务尚不支持后台重试")
        try:
            store.recover(jid)
            store.retry(jid)
            if runner is default_runner:
                worker.launch(workdir, jid)
            else:
                runner(lambda: worker.run_job(workdir, jid))
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f"/job/{jid}", 303)

    from . import experiment_ui
    experiment_ui.register(app, workdir, page=page, guard=guard, submit=submit,
                           token=token, markdown=md_to_html)
    from . import strategy_ui, paper_ui
    strategy_ui.register(app, workdir, page=page, guard=guard, token=token, locate=_run_dir)
    paper_ui.register(app, workdir, page=page, guard=guard, token=token)
    app.state.token = token
    app.state.store = store
    return app


def _local_time(iso_utc: str) -> str:
    """Job timestamps are stored in UTC; users read the computer's local time."""
    try:
        return datetime.fromisoformat(iso_utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_utc[:16].replace("T", " ")


def _job_rows(store: Store) -> str:
    rows = []
    label = {"queued": "排队中", "running": "运行中", "succeeded": "完成", "failed": "失败",
             "cancelled": "已取消"}
    for j in sorted(store.list(), key=lambda j: j.get("updated", ""), reverse=True):
        try:
            spec = store.spec(j["job_id"])
        except (OSError, ValueError):
            continue
        ui = spec.get("config", {}).get("ui")
        if not ui:
            continue
        kind = {"train": "训练", "audit": "体检外部模型", "bench": "Bench", "compare": "版本比较", "protocol": "冻结协议"}.get(ui.get("what"), "下载数据")
        rows.append(f'<tr><td><a href="/job/{j["job_id"]}">{html.escape(ui["name"])}</a></td>'
                    f'<td>{kind}</td><td>{label.get(j["state"], j["state"])}</td>'
                    f'<td>{_local_time(j["updated"])}</td></tr>')
        if len(rows) >= 20:
            break
    return "".join(rows) or '<tr><td colspan="4" class="mut">还没有记录</td></tr>'


def cn_a_factor_apis() -> dict:
    from .connectors import cn_a
    return cn_a.FACTOR_APIS


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
    print(f"WiseHold 工作台已启动：http://127.0.0.1:{port}  （关闭这个窗口即退出）", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


LAYOUT = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} · WiseHold</title>{{refresh}}
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<style>
@font-face{font-family:"DM Sans";src:url("/static/dm-sans.ttf") format("truetype");font-weight:100 1000;font-display:swap}
:root{--bg:#f5f6f8;--card:#fff;--fg:#15171d;--mut:#66707c;--line:#e6e8ed;--acc:#2855ed;--acch:#1744d5;--accbg:#edf1ff;
--ink:#171b24;--ok:#23794a;--okbg:#eaf6ef;--warn:#9a6200;--warnbg:#fdf5e4;--bad:#b23a1d;--badbg:#fdeee9;--code:#f1f3f7;
--shadow:0 1px 2px #1b27400a,0 8px 24px #263b570a;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#10131a;--card:#171b24;--fg:#e8ebf2;--mut:#8d97a8;--line:#2a3140;
--acc:#7d9bff;--acch:#9fb5ff;--accbg:#1c2540;--ink:#e8ebf2;--ok:#7fd0a0;--okbg:#15281e;--warn:#e8c27a;--warnbg:#2b2416;
--bad:#f09a82;--badbg:#2e1c17;--code:#1f2430;--shadow:0 1px 2px #0004;color-scheme:dark}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.7 "DM Sans",system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;letter-spacing:0;margin:0;padding:0 16px}
a{color:var(--acc);text-decoration:none}a:hover{color:var(--acch);text-decoration:underline}
.top{position:sticky;top:0;z-index:10;margin:0 -16px;padding:0 16px;background:color-mix(in srgb,var(--bg) 82%,transparent);
backdrop-filter:blur(18px) saturate(1.6);border-bottom:1px solid var(--line)}
.top-in{max-width:1040px;margin:0 auto;display:flex;align-items:center;gap:28px;min-height:60px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;font-size:16px;color:var(--fg);white-space:nowrap}
.brand:hover{text-decoration:none;color:var(--fg)}.brand img{width:26px;height:26px;border-radius:6px}.brand i{color:#a7aebb;font-style:normal}
.nav{display:flex;gap:4px;flex-wrap:wrap;margin:0}.nav a{font-size:14px;color:var(--mut);padding:7px 12px;border-radius:7px;white-space:nowrap;font-weight:500}
.nav a:hover{background:var(--card);color:var(--fg);text-decoration:none}.nav a.on{background:var(--accbg);color:var(--acc)}
main{max-width:1040px;margin:0 auto;padding-block:26px 64px}
.lede{color:var(--mut);font-size:13px;margin:0 0 6px}
h1{font-size:26px;font-weight:600;margin:0 0 6px}h2{font-size:21px;font-weight:600}h3{font-size:17px;font-weight:600}
h1,h2,h3{overflow-wrap:anywhere}
.step{border-top:1px solid var(--line);padding:24px 0;margin:0;scroll-margin-top:140px}
.step>h2{margin:0 0 12px;font-size:18px;display:flex;align-items:center}
.num{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;
background:var(--ink);color:var(--card);font-size:14px;font-weight:600;margin-right:10px;flex:none}
.card{display:block;border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:8px 0;cursor:pointer;transition:border-color .15s,background .15s}
.card:hover{border-color:color-mix(in srgb,var(--acc) 40%,var(--line))}
.card:has(input:checked){border-color:var(--acc);background:var(--accbg)}.card>span:last-child{display:block;color:var(--mut);margin-left:22px}.card .tag{display:inline-block}
.row{display:block;padding:7px 6px;border-radius:7px;cursor:pointer}.row:hover{background:var(--code)}
.q{margin:14px 0}.q>b{display:block;margin-bottom:4px}.opt{display:inline-block;margin:3px 16px 3px 0;cursor:pointer}
.opt small,.card small{color:var(--mut);margin-left:6px}.off{opacity:.5;cursor:not-allowed}
.tag{font-size:12px;border:1px solid var(--line);border-radius:5px;padding:0 6px;margin-left:6px;color:var(--mut);background:var(--code)}
textarea{font:inherit;width:100%;max-width:640px;margin-top:6px;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--card);color:var(--fg)}
details{margin:10px 0}summary{cursor:pointer;color:var(--acc);font-weight:500}
.sub{border:1px dashed var(--line);border-radius:10px;padding:12px 16px;margin:10px 0;background:color-mix(in srgb,var(--code) 50%,transparent)}
label.f{display:inline-block;margin:6px 18px 6px 0;max-width:calc(100% - 18px)}
input,select,button,textarea{font:inherit}
input,select{padding:7px 10px;max-width:100%;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 18%,transparent)}
input[type=number]{width:7.5em}input[type=radio],input[type=checkbox]{margin-right:6px;accent-color:var(--acc)}
button,.btn{background:var(--ink);color:var(--card);border:1px solid var(--ink);border-radius:8px;padding:9px 18px;cursor:pointer;
text-decoration:none;display:inline-block;font-weight:500;transition:background .15s,box-shadow .15s}
button:hover,.btn:hover{background:var(--acc);border-color:var(--acc);color:#fff;text-decoration:none;box-shadow:0 6px 18px color-mix(in srgb,var(--acc) 25%,transparent)}
button:focus-visible,.btn:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button.big{font-size:16px;padding:13px 30px;background:var(--acc);border-color:var(--acc);color:#fff}button.big:hover{background:var(--acch)}
button.secondary{background:transparent;color:var(--acc);border:1px solid var(--line)}button.secondary:hover{background:var(--accbg);color:var(--acc);box-shadow:none}
.mut{color:var(--mut)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.hw{font-size:13px;color:var(--mut);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.box{border-radius:12px;padding:14px 18px;margin:14px 0;background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow)}
.box.ok{background:var(--okbg);border-color:color-mix(in srgb,var(--ok) 25%,var(--line))}
.box.bad{background:var(--badbg);border-color:color-mix(in srgb,var(--bad) 25%,var(--line))}
td.ok{color:var(--ok)}td.bad{color:var(--bad)}.warnt{color:var(--warn);border-color:var(--warn)}
ul.verdict{padding-left:18px}ul.verdict li{margin:6px 0}li.ok{color:var(--ok)}li.warn{color:var(--warn)}li.bad{color:var(--bad)}
.state{font-size:18px;font-weight:600}.state.succeeded{color:var(--ok)}.state.failed{color:var(--bad)}
progress{width:260px;vertical-align:middle;accent-color:var(--acc)}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
.evaluation-list{min-width:620px}.evaluation-list td,.evaluation-list th{white-space:nowrap}
.holdings-table{min-width:620px}.holdings-table td,.holdings-table th{white-space:nowrap}
th{font-weight:600;color:var(--mut);font-size:13px;background:var(--code)}
td,th{border-bottom:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}
table tr:hover td{background:color-mix(in srgb,var(--accbg) 45%,transparent)}
.identity{display:grid;grid-template-columns:90px minmax(0,1fr);gap:6px 12px}.identity dd{margin:0;overflow-wrap:anywhere}
.identity code{word-break:break-all}.inline{display:inline}select{min-width:0}code{overflow-wrap:anywhere}
.page-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin:20px 0}.page-heading p{margin:4px 0 0}
.page-heading>div:first-child{min-width:0}.page-heading h1{font-size:24px}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:16px;flex-shrink:0}.actions form{margin:0}
.breadcrumb,.workspace-caption{font-size:12px;color:var(--mut)}.workspace-caption{margin:0 0 16px}.scope-label{font-size:12px;color:var(--mut)}
.notice{font-size:13px;color:var(--mut);border-left:2px solid var(--line);padding:8px 14px;margin:16px 0 24px}.notice.warn{border-color:var(--warn);color:var(--warn)}.notice ul{margin:0;padding-left:16px}
.account-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:24px 0}.account-card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:22px}
.card-heading,.card-meta,.section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.account-card h3,.account-card h2{font-size:18px;line-height:1.5;margin:14px 0 18px;overflow-wrap:anywhere}.account-card h3 a,.account-card h2 a{color:var(--fg)}
.status-badge{font-size:12px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}.status-badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.status-badge.pending{color:var(--warn)}.status-badge.active{color:var(--ok)}.status-badge.blocked{color:var(--bad)}
.metrics{display:grid;gap:20px;padding:20px 0}.compact-metrics{grid-template-columns:repeat(2,minmax(0,1fr));padding:8px 0 16px}.account-metrics{grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:24px 0}
.metric{min-width:0}.metric>span,.metric small{display:block;color:var(--mut);font-size:12px}.metric strong{display:block;font-size:24px;font-weight:550;line-height:1.4;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.metric small{min-height:18px;margin-top:3px}
.card-meta,.chart-dates{font-size:12px;color:var(--mut)}.account-message{font-size:13px;min-height:44px;margin:12px 0}.card-link{display:block;border-top:1px solid var(--line);padding-top:14px;font-size:13px}.evidence-note{font-size:13px;color:var(--warn);margin:0 0 16px}
.chart-plot{display:flex;gap:8px;align-items:stretch;min-width:0}.equity-chart{display:block;width:100%;height:200px;min-width:0}.equity-chart.compact{height:72px}.chart-scale{display:flex;flex-direction:column;justify-content:space-between;font-size:11px;color:var(--mut);padding:22px 0;flex:none}.chart-dates{display:flex;justify-content:space-between;margin:6px 0 14px;gap:12px}
.chart-empty{height:200px;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line);font-size:13px;color:var(--mut);text-align:center}.chart-empty.compact{height:72px;margin-bottom:28px}
.data-section{padding:10px 0 24px;min-width:0}.data-section h2{font-size:17px;margin:12px 0 18px}.data-section th,.data-section td{white-space:nowrap}.section-heading span{font-size:12px}.account-detail-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:28px}.empty-cell{height:80px;text-align:center;vertical-align:middle;color:var(--mut)}.empty-state{padding:60px 0;text-align:center;border-block:1px solid var(--line)}
.operation-details{border-top:1px solid var(--line);padding-top:16px;font-size:13px}.toolbar{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;border-block:1px solid var(--line);padding:14px 0;margin:22px 0}.toolbar label.f{margin:0}.toolbar input{display:block;margin-top:5px}
.scenario-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 30px}.scenario-grid .q{min-width:0}.scenario-grid select{width:100%}.scenario-grid small{font-size:12px}.model-choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.model-choices .card{margin:8px 0;min-height:130px}
.training-steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:24px 0 0;border-bottom:1px solid var(--line)}.training-steps a{padding:12px 0;font-size:13px;color:var(--mut)}.training-steps b{color:var(--acc);margin-right:10px}.hw-details{font-size:12px;margin:16px 0}.hw-details .hw{background:transparent;border:0;border-radius:0;padding:10px 0}
@media(max-width:760px){.account-detail-grid{grid-template-columns:minmax(0,1fr)}.account-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.page-heading{flex-wrap:wrap}.actions{flex-shrink:1}.account-grid{gap:14px}.account-card{padding:18px}.scenario-grid{grid-template-columns:minmax(0,1fr)}}
@media(max-width:600px){.account-grid,.model-choices{grid-template-columns:minmax(0,1fr)}.model-choices .card{min-height:0}.page-heading h1{font-size:22px}.training-steps a{font-size:12px}.training-steps b{display:block}.metrics{gap:14px}.metric strong{font-size:22px}}
pre.plain{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:12px;border-radius:8px;font-size:13px}
code{background:var(--code);padding:1px 6px;border-radius:5px;font-size:13px}
svg{max-width:100%}
.foot{max-width:1040px;margin:0 auto;padding:18px 0 28px;border-top:1px solid var(--line);color:var(--mut);font-size:12px}
@media(max-width:640px){.top-in{gap:8px;padding-block:8px}.nav a{padding:6px 9px;font-size:13px}.step{padding:22px 0}main{padding-block:18px 48px}h1{font-size:22px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<header class="top"><div class="top-in">
<a class="brand" href="/" aria-label="WiseHold 首页"><img src="/static/favicon.svg" alt=""><span>WiseHold</span></a>
<nav class="nav" aria-label="主导航"><a href="/">训练模型</a><a href="/experiments">实验记录</a><a href="/models">使用模型</a><a href="/paper">模拟账户</a><a href="/audit">体检外部模型</a></nav>
</div></header>
<main>
<p class="workspace-caption">本地工作台 / A 股</p>
{{body}}
</main>
<footer class="foot">本地运行 · 127.0.0.1 · 结果只说明在给定数据和假设下得到了什么，不代表未来收益。</footer>
<script>(function(){var p=location.pathname,best=null;document.querySelectorAll(".nav a").forEach(function(a){var h=a.getAttribute("href");
var hit=h==="/"?(p==="/"||p.indexOf("/job")===0||p.indexOf("/data")===0):p.indexOf(h)===0;
if(hit&&(!best||h.length>best.getAttribute("href").length))best=a});
if(best){best.classList.add("on");best.setAttribute("aria-current","page")}})();</script>
</body></html>"""

INDEX = """
<div class="page-heading"><div><h1>训练模型</h1></div><a href="/models">已有模型</a></div>
<nav class="training-steps" aria-label="训练步骤"><a href="#step1"><b>01</b>训练目标</a><a href="#step2"><b>02</b>选择数据</a><a href="#step3"><b>03</b>参数确认</a><a href="#step4"><b>04</b>开始训练</a></nav>
<details class="hw-details"><summary>本机资源 · {mem} 内存 · CPU 训练</summary>
<p class="hw">本机：{cpu}；内存 {mem}；显卡 {gpu}。<b>LightGBM 和 Ridge 只用 CPU，什么显卡（含 AMD、核显、没有显卡）都不影响。</b>树模型做的是排序和比大小，CPU 本来就擅长；只有神经网络底座（如 Kronos）才需要 NVIDIA 显卡。</p>
</details>
{memwarn}
<form method="post" action="/run" id="main">
<input type="hidden" name="token" value="{token}">

<section class="step" id="step1"><h2><span class="num">1</span>要打造什么模型：用在哪</h2>
<p class="mut">先说清楚模型要用在什么地方。后面的数据要求和参数都由这里决定。灰色的是规划中、暂时不能选。</p>
{step1}
</section>

<section class="step" id="step2"><h2><span class="num">2</span>用什么数据</h2>
{datasets}
<p class="mut">还没有数据？用下面任意一种方式添加，添加后会出现在上面的列表里。
一份数据还可以叠加多个来源（本地 CSV 和 API 混用）：点数据后面的“加更多数据来源”。</p>
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

<details><summary>添加数据 · 方式 C：使用电脑上已有的 WiseHold 数据文件夹</summary><div class="sub">
<form method="post" action="/data/folder">
<input type="hidden" name="token" value="{token}">
<label class="f">文件夹路径 <input name="path" size="40" placeholder="例如 E:\\data\\wisehold_data" required></label>
<button>添加</button></form></div></details>
</section>

<section class="step" id="step3"><h2><span class="num">3</span>参数（由第 1 步推出，可以不改）</h2>
<label class="f">给这次训练起个名字 <input form="main" name="name" value="{name}" required
 pattern="[A-Za-z0-9_\\-]{{1,40}}"></label>
<div class="derived-summary" id="derived"></div>
<details><summary>修改参数（不填就用上面的值；改过的会写在报告首页）</summary><div class="sub">
<p><b>持仓与成本</b></p>
<label class="f">持股只数 <input form="main" type="number" name="n_hold" id="n_hold" min="1" max="500"></label>
<label class="f">佣金（万分之几，单边） <input form="main" type="number" name="commission_bp" placeholder="{comm}" step="0.5" min="0"></label>
<div class="pm pm-lightgbm"><p><b>LightGBM 参数</b></p>
<label class="f">树的数量 <input form="main" type="number" name="num_boost_round" placeholder="{rounds}" min="10" max="5000"></label>
<label class="f">叶子数 <input form="main" type="number" name="num_leaves" placeholder="{leaves}" min="2" max="1024"></label>
<label class="f">学习率 <input form="main" type="number" name="learning_rate" placeholder="{lr}" step="0.005" min="0.001" max="1"></label>
<label class="f">重复训练次数（种子） <input form="main" type="number" name="n_seeds" placeholder="{seeds}" min="1" max="10"></label>
<p class="mut">叶子越多、树越多，模型越复杂，越容易记住历史噪声。</p></div>
<div class="pm pm-ridge" hidden><p><b>Ridge 参数</b></p>
<label class="f">正则强度 <input form="main" type="number" name="ridge_alpha" placeholder="{alpha}" step="0.1" min="0.01"></label>
<p class="mut">越大越保守。Ridge 每次结果相同，不需要重复训练。</p></div>
<p class="mut">训练和测试年份按数据自动设定：最后两年作为最终测试期（数据少于 5 年时用最后一年）。</p>
</div></details>
</section>

<section class="step" id="step4"><h2><span class="num">4</span>开始</h2>
<button class="big" form="main">开始训练</button>
<span class="mut">　训练在本机进行，完成后自动给出报告和一句话结论。</span>
</section>

<h3>运行记录</h3>
<div class="tw"><table><tr><th>名称</th><th>类型</th><th>状态</th><th>更新时间</th></tr>{jobs}</table></div>
"""


SCRIPT = """<script>
(function () {
  var T = __TIERS__, PART = __PART__;
  var HZ = {"1": "持有 1 天，每天换仓，换手和成本最高", "5": "持有 5 天，每周换仓",
            "20": "持有 20 天，每月换仓；历史样本少，结果更不稳定"};
  function val(n) { var e = document.querySelector('[name="' + n + '"]:checked'); return e ? e.value : ""; }
  function wan(x) { return x >= 1e8 ? (x / 1e8).toFixed(x >= 1e9 ? 0 : 1) + " 亿" : Math.round(x / 1e4).toLocaleString() + " 万"; }
  function upd() {
    var t = T[document.getElementById("capital").value];
    var nh = document.getElementById("n_hold"), n = parseInt(nh.value) || t.n_hold;
    nh.placeholder = t.n_hold;
    var floor = t.capital / n / PART, model = val("model") || "lightgbm";
    document.getElementById("capnote").textContent =
      "按 " + t.label + " 计：默认持有 " + t.n_hold + " 只，每只约 " + wan(t.capital / t.n_hold) + "。";
    var u = document.getElementById("universe");
    document.getElementById("listbox").hidden = u.value !== "list";
    document.getElementById("derived").innerHTML =
      "<b>因为你在第 1 步的选择，默认这样训练：</b><ul>" +
      "<li>标签：第二天开盘买入，" + HZ[val("horizon") || "5"] + "。</li>" +
      "<li>持仓：" + n + " 只等权（资金 " + t.label + "）。</li>" +
      "<li>只选近 20 日日均成交额 ≥ " + wan(floor) + "元的股票，保证每只仓位不超过它当天成交额的 " +
      (PART * 100) + "%，买得进卖得出。</li>" +
      "<li>股票池：" + u.options[u.selectedIndex].text + "。</li>" +
      "<li>模型：" + (model === "ridge" ? "Ridge 线性回归，训练 1 次" : "LightGBM，按年滚动重训") + "。</li></ul>";
    document.querySelectorAll(".pm").forEach(function (b) { b.hidden = !b.classList.contains("pm-" + model); });
  }
  document.querySelectorAll("[form=main], #main input").forEach(function (e) { e.addEventListener("change", upd); });
  document.getElementById("n_hold").addEventListener("input", upd);
  upd();
})();
</script>"""
