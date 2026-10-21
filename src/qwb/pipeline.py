"""One run end to end, from a resolved run config (qwb.runconfig).

load data -> walk-forward training -> baselines and tests -> prediction table
-> report. Used by `qwb run <config.yaml>`; scripts/m2_run.py is a thin wrapper.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from . import daily, env, metrics

TZ = {"cn_a": "Asia/Shanghai", "us": "America/New_York"}


def synth_bars(n_days: int = 700, n_codes: int = 300, seed: int = 0) -> pd.DataFrame:
    """Synthetic panel with a weak 5-day reversal; checks the pipeline, not a market."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n_days)
    ret = rng.normal(0, 0.02, (n_days, n_codes))
    for t in range(5, n_days):
        ret[t] -= 0.05 * ret[t - 5:t].sum(axis=0)
    close = 10 * np.exp(np.cumsum(ret, axis=0))
    d = np.repeat(dates, n_codes)
    c = np.tile([f"{i:06d}.SZ" for i in range(n_codes)], n_days)
    cl = close.ravel()
    op = cl * np.exp(rng.normal(0, 0.002, cl.shape))
    return pd.DataFrame({"code": c, "date": d, "open": op, "high": np.maximum(op, cl) * 1.01,
                         "low": np.minimum(op, cl) * 0.99, "close": cl,
                         "amount": 1e7 * (1 + np.tile(np.arange(n_codes), n_days)),
                         "adj_factor": 1.0, "up_limit": cl * 1.1})


def find_local_config(start: Path) -> dict:
    """config.local.yaml in the working directory or any parent; {} if none."""
    for d in [start, *start.parents]:
        p = d / "config.local.yaml"
        if p.is_file():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def load_data(rc: dict, cwd: Path) -> tuple[dict, str, pd.DataFrame | None]:
    """(panel, snapshot id, ST mask or None)."""
    src = rc["data"]
    cfg = rc["cfg"]
    if src["source"] == "synthetic":
        bars = synth_bars(src.get("n_days") or 700, src.get("n_codes") or 300)
        if cfg["universe"] != "all":
            raise ValueError("synthetic data has no ST status; use universe: all")
        return daily.panel_from_bars(bars), "snap-synthetic", None
    data_dir = src.get("dir") or find_local_config(cwd).get("data_dir")
    if not data_dir:
        raise FileNotFoundError("data.dir not set and no data_dir in config.local.yaml")
    data_dir = Path(data_dir)
    snap = daily.load_snapshot(data_dir)["data_snapshot_id"]
    P = daily.load_panel(data_dir)
    st = None
    if cfg["universe"] != "all":
        status = pd.read_parquet(data_dir / "st_status.parquet")
        st = daily.st_wide(status, P["close"].index, P["close"].columns)
    return P, snap, st


def _summ(ic, dates, years, lag):
    return metrics.newey_west(ic[np.isin(dates.year, years)], lag)


def _fmt(s: dict) -> str:
    if not s["n"]:
        return "n/a"
    return f"{s['mean']:+.4f} (t={s['t']:.2f}, MDE={s['mde']:.4f}, n={s['n']})"


def run(rc: dict, cwd: Path, log: Callable[[str], None] = print) -> dict:
    card, cfg = rc["card"], rc["cfg"]
    out = (cwd / rc["output"]).resolve()
    (out / "models").mkdir(parents=True, exist_ok=True)
    tz = TZ.get(card["market"], "UTC")
    T = {}
    t0 = time.time()

    # acceptance is locked before the final test years are first scored (SPEC §6)
    acc = json.dumps(card.get("acceptance", {}), sort_keys=True, ensure_ascii=False)
    acc_hash = hashlib.sha256(acc.encode("utf-8")).hexdigest()[:16]
    lock = out / "acceptance.lock"
    acc_changed = lock.exists() and lock.read_text(encoding="utf-8").strip() != acc_hash
    if not lock.exists():
        lock.write_text(acc_hash, encoding="utf-8")
    (out / "task.yaml").write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")
    (out / "config.resolved.yaml").write_text(yaml.safe_dump(
        {k: rc[k] for k in ("name", "data", "model", "cfg", "config_hash", "deviations")},
        allow_unicode=True, sort_keys=False), encoding="utf-8")

    P, snap, st = load_data(rc, cwd)
    T["load"] = time.time() - t0
    dates = P["close"].index
    log(f"panel {len(dates)} days x {P['close'].shape[1]} codes, load {T['load']:.1f}s")

    t1 = time.time()
    res = daily.run(P, card, cfg, snap, tz, st=st, model_dir=out / "models", log=log)
    T["train_and_predict"] = time.time() - t1

    t2 = time.time()
    y = res["y"]
    lag = int(card["target"].get("horizon_days") or
              (int(card["label"]["end"]["day"]) - int(card["label"]["start"]["day"])))
    seeds = list(res["pred"])
    ens = daily.ensemble(res["pred"])
    test_years = sorted({d.year for d in dates if d.year >= cfg["first_test_year"]})
    final = [yy for yy in cfg["final_test_years"] if yy in test_years]
    valid = [yy for yy in test_years if yy not in final]
    if not final:
        raise ValueError(f"final_test_years {cfg['final_test_years']} not in the data")

    raw = daily.raw_features(P)
    tested = np.isin(dates.year, test_years)[:, None] & res["U"]
    base = {"reversal_5d (-r5)": -raw["r5"].values, "momentum_20d (r20)": raw["r20"].values,
            "low_vol (-vol20)": -raw["vol20"].values}
    ic = {"lgbm_ensemble": metrics.rank_ic(ens, y)}
    for s in seeds:
        ic[f"lgbm_seed{s}"] = metrics.rank_ic(res["pred"][s], y)
    for k, v in base.items():
        ic[k] = metrics.rank_ic(np.where(tested, v, np.nan), y)
    rows = {k: {"validation": _summ(v, dates, valid, lag), "final_test": _summ(v, dates, final, lag)}
            for k, v in ic.items()}
    fin = np.isin(dates.year, final)
    null = metrics.shuffle_null(np.where(fin[:, None], ens, np.nan), y,
                                n=cfg["shuffle_null_draws"])
    seed_corr, seed_overlap = [], []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            a, b = res["pred"][seeds[i]], res["pred"][seeds[j]]
            seed_corr.append(float(np.nanmean(metrics.rank_ic(a, b))))
            seed_overlap.append(metrics.top_overlap(a, b, 0.1))
    T["evaluate"] = time.time() - t2

    t3 = time.time()
    pred_tab = daily.prediction_table(res, card, snap, tz)
    pred_tab.to_parquet(out / "predictions.parquet", index=False)
    (out / "folds.json").write_text(json.dumps(res["manifests"], indent=2), encoding="utf-8")
    T["write"] = time.time() - t3
    T["total"] = time.time() - t0

    best_base = max(rows[k]["final_test"]["mean"] for k in base)
    f = rows["lgbm_ensemble"]["final_test"]
    go = bool(f["n"] and f["mean"] > 0 and f["t"] > 2 and f["mean"] > best_base)
    meta = env.metadata()
    summary = {"task": card["name"], "run": rc["name"], "config_hash": rc["config_hash"],
               "data_snapshot_id": snap, "universe": cfg["universe"],
               "panel": [len(dates), int(P["close"].shape[1])], "test_years": test_years,
               "final_test_years": final, "nw_lag": lag, "ic": rows,
               "shuffle_null_mean": float(np.mean(null)), "shuffle_null_sd": float(np.std(null)),
               "seed_pred_rank_corr": seed_corr, "seed_top10_overlap": seed_overlap,
               "go": go, "acceptance_hash": acc_hash, "acceptance_changed_after_lock": acc_changed,
               "deviations": rc["deviations"], "timing_s": {k: round(v, 1) for k, v in T.items()},
               "env": meta, "rows_predicted": int(len(pred_tab))}
    (out / "metrics.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(_report(rc, summary, rows, null, seed_corr, seed_overlap,
                                           valid, final, T, meta, len(seeds)), encoding="utf-8")
    summary["output"] = str(out)
    return summary


def _report(rc, s, rows, null, seed_corr, seed_overlap, valid, final, T, meta, n_seeds) -> str:
    card, cfg = rc["card"], rc["cfg"]
    devs = rc["deviations"]
    g = meta["gpu"]
    synthetic = rc["data"]["source"] == "synthetic"
    lines = [
        f"# 报告：{rc['name']}{'（合成数据，只验证流程）' if synthetic else ''}", "",
        f"任务 `{card['name']}`　数据快照 `{s['data_snapshot_id']}`　股票池 `{cfg['universe']}`　"
        f"面板 {s['panel'][0]} 日 × {s['panel'][1]} 只　模型 LightGBM × {n_seeds} 种子，按年滚动重训　"
        f"配置 `{rc['config_hash']}`", "",
        "## 首页标注",
        f"- 验收门槛哈希 `{s['acceptance_hash']}`"
        + ("；**门槛在锁定后被修改**" if s["acceptance_changed_after_lock"] else "，锁定后未改"),
        "- 偏离默认值：" + ("; ".join(devs) if devs else "无"),
        "- 未执行：组合构建、成本、成交约束（首版只输出预测表，SPEC §10）",
        "- 尝试次数：本地台账自报，不作认证（SPEC §14）", "",
        f"## 结果（日度 Rank IC 均值，Newey-West t，lag 事先固定为 {s['nw_lag']}；"
        "MDE 为 80% 功效下可检出的最小均值）", "",
        f"| 信号 | 验证期 {valid[0] if valid else ''}–{valid[-1] if valid else ''} | "
        f"最终测试 {final[0]}–{final[-1]} |", "|---|---|---|"]
    lines += [f"| {k} | {_fmt(r['validation'])} | {_fmt(r['final_test'])} |" for k, r in rows.items()]
    lines += ["", f"随机信号基线（截面内打乱，{cfg['shuffle_null_draws']} 次）：最终测试 IC "
              f"{np.mean(null):+.4f} ± {np.std(null):.4f}"]
    if seed_corr:
        lines.append(f"种子噪声地板：种子间预测秩相关 {np.mean(seed_corr):.3f}，前 10% 持仓重叠 "
                     f"{np.mean(seed_overlap):.3f}")
    lines += ["", f"**预注册判定：{'Go' if s['go'] else 'No-Go'}**"
              f"（{card.get('acceptance', {}).get('go', '未写门槛')}）", "",
              "## 耗时（秒）", "", "| 阶段 | 秒 |", "|---|---|"]
    lines += [f"| {k} | {v:.1f} |" for k, v in T.items()]
    lines += ["", f"环境：Python {meta['python']}，"
              + (f"{g['name']}（LightGBM 走 CPU，GPU 未使用）" if g else "无 GPU")
              + f"，lightgbm {meta['packages'].get('lightgbm')}", "",
              "## 证据边界",
              "本报告只说明在给定数据、假设和配置下的预测排序能力；不含成本与成交约束，不代表可实现收益。"]
    return "\n".join(lines)
