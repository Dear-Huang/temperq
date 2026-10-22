"""Check-up for a model trained anywhere else (Qlib, vn.py alpha, a notebook…).

Input: a prediction table, one row per stock and decision day:

    code, date, score            # date = the day the score is known (after its close)

The scores are judged on the user's data with qwb's rules: the same tradable
labels, baselines, shuffle null, net-of-cost test, the comparison against
Qlib's default backtest settings, and the parameter rule book. Nothing about
the model itself is needed, so it works for any tool.

Qlib's `pred.pkl` / `pred.csv` (index datetime, instrument; column score) is
accepted as CSV with columns datetime, instrument, score; instrument codes like
SH600000 are converted.
"""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import advice, daily, env, ledger, metrics, netcost, scenario

ALIASES = {"datetime": "date", "trade_date": "date", "instrument": "code", "ts_code": "code",
           "代码": "code", "股票代码": "code", "日期": "date", "交易日期": "date",
           "pred": "score", "prediction": "score", "预测": "score", "打分": "score", "得分": "score"}
MIN_DAYS = 60
MIN_NAMES = 30


class AuditError(ValueError):
    """User-facing problem with the uploaded predictions."""


def _code(s: str) -> str:
    s = str(s).strip().upper()
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)               # Qlib style
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    from .csvimport import _suffix
    return _suffix(s)


def read_predictions(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding_errors="replace")
    df.columns = [ALIASES.get(str(c).strip().lower(), ALIASES.get(str(c).strip(),
                                                              str(c).strip().lower())) for c in df.columns]
    if "score" not in df.columns:
        num = [c for c in df.columns if c not in ("code", "date")
               and pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9]
        if len(num) == 1:
            df = df.rename(columns={num[0]: "score"})
    missing = [c for c in ("code", "date", "score") if c not in df.columns]
    if missing:
        raise AuditError(f"缺少列：{', '.join(missing)}。需要 code（股票代码）、date（打分日期）、score（打分）")
    d = pd.to_datetime(df["date"].astype(str).str.strip(), format="mixed", errors="coerce")
    if d.isna().mean() > 0.01:
        raise AuditError("日期列看不懂，请用 2024-01-02 或 20240102")
    out = pd.DataFrame({"code": df["code"].map(_code), "date": d.dt.normalize(),
                        "score": pd.to_numeric(df["score"], errors="coerce")}).dropna()
    if out.duplicated(["code", "date"]).any():
        raise AuditError("同一只股票同一天有多个打分，请每天每只只保留一个")
    return out


def run(raw: bytes, filename: str, data_dir: Path | None, horizon: int, tier: str, out: Path,
        cwd: Path, name: str, universe: str = "all", log: Callable[[str], None] = print) -> dict:
    from .pipeline import load_data, synth_bars

    t0 = time.time()
    preds = read_predictions(raw)
    card = scenario.task_card(horizon)
    der = scenario.derive(tier)
    cfg = dict(daily.DEFAULTS, universe=universe, min_adv_yuan=round(der["min_adv_yuan"]),
               portfolio=dict(daily.DEFAULTS["portfolio"], n_hold=der["n_hold"]))
    data = {"source": "snapshot", "dir": str(data_dir)} if data_dir else {"source": "synthetic"}
    P, snap, st, members = load_data({"data": data, "cfg": cfg}, cwd)
    dates, codes = P["close"].index, P["close"].columns
    W = preds.pivot(index="date", columns="code", values="score").reindex(index=dates, columns=codes)
    sig = W.to_numpy(dtype="float64")
    U = daily.universe(P, cfg, st, members).to_numpy()
    has = (~np.isnan(sig) & U).sum(axis=1) >= MIN_NAMES
    if has.sum() < MIN_DAYS:
        hit = preds["code"].isin(codes).mean()
        raise AuditError(f"只有 {int(has.sum())} 个交易日有足够的打分（每天至少 {MIN_NAMES} 只、在股票池内），"
                         f"至少需要 {MIN_DAYS} 天。代码匹配率 {hit:.0%}；检查日期范围是否落在数据内")
    days = dates[has]
    years = sorted({d.year for d in days})
    y = daily.labels(P, card).where(pd.DataFrame(U, index=dates, columns=codes)).to_numpy()
    sig = np.where(has[:, None] & U, sig, np.nan)
    lag = horizon
    raw_f = daily.raw_features(P)
    in_p = has[:, None] & U
    base = {"reversal_5d (-r5)": -raw_f["r5"].values, "momentum_20d (r20)": raw_f["r20"].values,
            "low_vol (-vol20)": -raw_f["vol20"].values}
    ic = {"uploaded": metrics.rank_ic(sig, y),
          **{k: metrics.rank_ic(np.where(in_p, v, np.nan), y) for k, v in base.items()}}
    rows = {k: {"validation": {"n": 0}, "final_test": metrics.newey_west(v[has], lag)}
            for k, v in ic.items()}
    null = metrics.shuffle_null(sig, y, n=10)
    pf = cfg["portfolio"]
    net = netcost.evaluate(P, card, {"uploaded": sig, **{k: np.where(in_p, v, np.nan)
                                                          for k, v in base.items()}},
                           U, y, years, top=pf["top"], commission_bp=pf["commission_bp"],
                           n_hold=pf["n_hold"])
    conv = netcost.compare_conventions(P, card, sig, U, y, years, top=pf["top"],
                                       commission_bp=pf["commission_bp"], n_hold=pf["n_hold"])
    # a score that moves with future returns *before* its date is a leak, not skill
    y_past = daily.labels(P, card).shift(horizon + 1).to_numpy()
    past_ic = metrics.newey_west(metrics.rank_ic(sig, np.where(U, y_past, np.nan))[has], lag)
    f = rows["uploaded"]["final_test"]
    best_base = max(rows[k]["final_test"]["mean"] for k in base)
    s = {"task": card["name"], "run": name, "model": "external", "model_key": "uploaded",
         "config_hash": "external:" + filename[:40], "data_snapshot_id": snap,
         "universe": universe, "panel": [len(dates), len(codes)], "final_test_years": years,
         "test_years": years, "nw_lag": lag, "ic": rows, "days_scored": int(has.sum()),
         "code_match": float(preds["code"].isin(codes).mean()),
         "shuffle_null_mean": float(np.mean(null)), "shuffle_null_sd": float(np.std(null)),
         "net_of_cost": net, "conventions": conv, "past_return_ic": past_ic,
         "go": bool(f["n"] and f["mean"] > 0 and f["t"] > 2 and f["mean"] > best_base),
         "scenario": {"market": "A 股", "horizon": scenario.HORIZONS[horizon],
                      "universe": scenario.UNIVERSES.get(universe, universe),
                      "capital": der["label"], "model": f"外部模型（{filename}）"},
         "advice": advice.check(cfg, card, len(set(dates.year))),
         "timing_s": {"total": round(time.time() - t0, 1)}, "env": env.metadata()}
    if snap != "snap-synthetic":
        led = cwd / "runs" / "ledger.jsonl"
        s["ledger"] = ledger.history(led, card["name"], snap, years, s["config_hash"], f["mean"])
        ledger.append(led, {"run": name, "task": card["name"], "snapshot": snap,
                            "config_hash": s["config_hash"], "final_test_years": years,
                            "final_ic": f["mean"], "final_t": f["t"], "external": True,
                            "net_excess": net["signals"]["uploaded"]["net_excess_ann"],
                            "deviations": []})
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(s, indent=2, default=str, ensure_ascii=False),
                                      encoding="utf-8")
    (out / "report.md").write_text(report(s, cfg), encoding="utf-8")
    log(f"external audit: {int(has.sum())} days, IC {f['mean']:+.4f}")
    return s


def report(s: dict, cfg: dict) -> str:
    from .pipeline import _convention_lines, _fmt, _net_section, _scenario_lines
    f = s["ic"]["uploaded"]["final_test"]
    pi = s["past_return_ic"]
    y0, y1 = s["final_test_years"][0], s["final_test_years"][-1]
    lines = [f"# 外部模型体检：{s['run']}", "",
             f"数据快照 `{s['data_snapshot_id']}`　打分覆盖 {s['days_scored']} 个交易日（{y0}–{y1}）　"
             f"股票代码匹配 {s['code_match']:.0%}", "",
             *_scenario_lines(cfg), *_net_section(s["net_of_cost"], cfg, s["final_test_years"]),
             *_convention_lines(s["conventions"]),
             "## 前视嫌疑检查", "",
             f"打分和**打分日之前**那段收益的相关：{_fmt(pi)}。正常的模型可以和过去收益相关（比如反转、动量），"
             "但如果下一节的“未来 IC”高得不正常，同时打分里带着当天收盘后才知道的信息，就要检查打分日期是否写早了一天。",
             "", "## 结果（日度 Rank IC 均值，Newey-West t）", "",
             "| 信号 | 打分期间 |", "|---|---|",
             *[f"| {k} | {_fmt(v['final_test'])} |" for k, v in s["ic"].items()],
             "", f"随机信号基线：{s['shuffle_null_mean']:+.4f} ± {s['shuffle_null_sd']:.4f}", "",
             f"**判定：{'Go' if s['go'] else 'No-Go'}**（IC > 0 且 t > 2，且高于所有简单方法）", "",
             *advice.as_markdown(s["advice"]),
             "## 说明", "",
             "只用上传的打分和你自己的行情数据，不需要模型文件。打分期间整段都按测试期对待；"
             "如果这些日期也参与过模型训练或调参，结果会偏乐观，这一点本工具无法检查。", "",
             "## 证据边界", "本报告只说明在给定数据和假设下的排序与可成交表现，不构成投资建议。"]
    return "\n".join(lines)
