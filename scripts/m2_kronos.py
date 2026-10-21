"""M2 second model: Kronos-small zero-shot vs LightGBM on the same decision days.

Usage:  python -X utf8 scripts/m2_kronos.py [--every 5] [--lookback 120]
Runs as a resumable job: one unit per decision day (per-day parquet under the
job's artifacts). Writes runs/m2/cn_rank_5d/kronos_report.md at the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401  (torch before pandas)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from qwb import daily, metrics, taskcard  # noqa: E402
from qwb.jobs import JobSpec, Store  # noqa: E402

RUN = ROOT / "runs" / "m2" / "cn_rank_5d"
MODEL = {"name": "NeoQuasar/Kronos-small", "revision": "901c26c1332695a2a8f243eb2f37243a37bea320",
         "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
         "tokenizer_revision": "0e0117387f39004a9016484a186a908917e22426",
         "code": "shiyu-coder/Kronos@67b630e67f6a18c9e9be918d9b4337c960db1e9a", "license": "MIT"}


def setup(cfg):
    local = yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8"))
    data_dir = Path(local["data_dir"])
    card = taskcard.load(ROOT / "examples" / "cn_rank_5d.yaml")
    P = daily.load_panel(data_dir)
    status = pd.read_parquet(data_dir / "st_status.parquet")
    st = daily.st_wide(status, P["close"].index, P["close"].columns)
    dcfg = dict(daily.DEFAULTS, universe="all_ex_st")
    U = daily.universe(P, dcfg, st)
    dates = P["close"].index
    test = np.flatnonzero(np.isin(dates.year, dcfg["final_test_years"]))
    # decision days: every k-th test day, leaving room for the label window
    horizon = int(card["label"]["end"]["day"])
    days = [t for t in test[::cfg["every"]] if t + horizon < len(dates)]
    return P, U, card, dates, days, daily.load_snapshot(data_dir)["data_snapshot_id"]


def job(ctx, cfg):
    from qwb.adapters.kronos import KronosScorer

    P, U, card, dates, days, _ = setup(cfg)
    scorer = KronosScorer(ROOT, "models/kronos-small", "models/kronos-tokenizer-base",
                          max_context=cfg["max_context"])
    L = cfg["lookback"]
    full = P["close"].notna().rolling(L).sum().eq(L).to_numpy()   # no gaps in the lookback
    out_dir = ctx.artifacts / "days"
    out_dir.mkdir(exist_ok=True)
    for i, t in enumerate(days):
        ctx.check_cancel()
        unit = f"day_{dates[t]:%Y%m%d}"
        if ctx.unit_done(unit):
            continue
        cols = P["close"].columns
        codes = list(cols[U.to_numpy()[t] & full[t]])
        s0 = time.time()
        sc = scorer.score_day(P, t, codes, L, card, dates[t + 1:], batch=cfg["batch"],
                              seed=cfg["seed"])
        pd.DataFrame({"code": sc.index, "date": dates[t], "kronos": sc.values}) \
            .to_parquet(out_dir / f"{unit}.parquet", index=False)
        ctx.mark_done(unit)
        ctx.progress(i + 1, len(days), f"{dates[t]:%Y-%m-%d} n={len(codes)} {time.time() - s0:.0f}s")


def report(jid, store, cfg):
    P, U, card, dates, days, snap = setup(cfg)
    files = sorted((store.root / jid / "artifacts" / "days").glob("*.parquet"))
    k = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    k["date"] = pd.to_datetime(k["date"])
    cols = P["close"].columns
    K = k.pivot(index="date", columns="code", values="kronos").reindex(columns=cols)
    lg = pd.read_parquet(RUN / "predictions.parquet")
    lg["date"] = pd.to_datetime(lg["decision_time"].str[:10])
    G = lg.pivot(index="date", columns="code", values="prediction").reindex(index=K.index,
                                                                             columns=cols)
    y = daily.labels(P, card).where(U).reindex(index=K.index, columns=cols).to_numpy()
    raw = daily.raw_features(P)
    common = ~np.isnan(K.to_numpy()) & ~np.isnan(G.to_numpy())   # same names for both
    sig = {
        "kronos_small (zero-shot)": K.to_numpy(),
        "lgbm_ensemble": G.to_numpy(),
        "low_vol (-vol20)": -raw["vol20"].reindex(index=K.index, columns=cols).to_numpy(),
        "reversal_5d (-r5)": -raw["r5"].reindex(index=K.index, columns=cols).to_numpy(),
    }
    sig = {n: np.where(common, v, np.nan) for n, v in sig.items()}
    # decision days are 5 apart, labels do not overlap: NW lag 0 is the plain t-test;
    # lag 1 kept as a guard against residual dependence
    rows = {}
    for n, v in sig.items():
        ic = metrics.rank_ic(v, y)
        h2 = K.index >= "2025-07-01"
        rows[n] = {"all": metrics.newey_west(ic, 1), "2025H2": metrics.newey_west(ic[h2], 1)}
    corr = float(np.nanmean(metrics.rank_ic(sig["kronos_small (zero-shot)"],
                                            sig["lgbm_ensemble"])))

    # SPEC §9: paired difference on the same days and names, and incremental value
    kr, lgb_ = sig["kronos_small (zero-shot)"], sig["lgbm_ensemble"]
    ic_k, ic_g = metrics.rank_ic(kr, y), metrics.rank_ic(lgb_, y)
    paired = metrics.newey_west(ic_g - ic_k, 1)

    def rank01(a):
        return pd.DataFrame(a).rank(axis=1, pct=True).to_numpy()

    blends = {}
    for w in (0.1, 0.2, 0.3):
        b = (1 - w) * rank01(lgb_) + w * rank01(kr)
        blends[w] = metrics.newey_west(metrics.rank_ic(b, y) - ic_g, 1)
    # Kronos residual after removing its cross-sectional linear part explained by LightGBM
    rk, rg = rank01(kr) - 0.5, rank01(lgb_) - 0.5
    beta = np.nansum(rk * rg, axis=1, keepdims=True) / np.nansum(rg * rg, axis=1, keepdims=True)
    resid = np.where(np.isnan(rk) | np.isnan(rg), np.nan, rk - beta * rg)
    resid_ic = metrics.newey_west(metrics.rank_ic(resid, y), 1)

    def fmt(s):
        return f"{s['mean']:+.4f} (t={s['t']:.2f}, MDE={s['mde']:.4f}, n={s['n']})" if s["n"] else "n/a"

    n_names = int(np.nanmean(common.sum(axis=1)))
    lines = [
        "# M2 第二模型：Kronos-small 零样本 vs LightGBM", "",
        f"数据快照 `{snap}`　任务 `{card['name']}`（open[t+1]→open[t+6]）　股票池 all_ex_st，"
        f"且要求 {cfg['lookback']} 日回看无缺口", "",
        f"决策日：2024–2025 每 {cfg['every']} 个交易日取 1 天，共 {len(K)} 天，标签不重叠；"
        f"每天两模型用同一批股票，平均 {n_names} 只", "",
        f"模型：{MODEL['name']}@{MODEL['revision'][:7]}，tokenizer {MODEL['tokenizer']}@"
        f"{MODEL['tokenizer_revision'][:7]}，代码 {MODEL['code'].split('@')[0]}@"
        f"{MODEL['code'].split('@')[1][:7]}，"
        f"{MODEL['license']}。零样本（未微调），lookback {cfg['lookback']}，sample_count=1，"
        "T=1.0，top_p=0.9，种子固定", "",
        "## 首页标注",
        "- **预训练污染风险**：Kronos 官方未公开预训练数据截止日（论文 2025-08 发布，权重 2025-09 更新）。"
        "2024–2025 测试区间很可能落在预训练数据内，零样本结果可能偏乐观。2025H2 单列，作为相对干净的窗口参照，"
        "但同样无法确认", "",
        "- 单次采样（sample_count=1），预测带随机性；未测多次采样的离散度", "",
        "## 结果（Rank IC 均值，NW lag 1）", "",
        "| 信号 | 2024–2025 抽样日 | 其中 2025H2 |", "|---|---|---|"]
    for n, r in rows.items():
        lines.append(f"| {n} | {fmt(r['all'])} | {fmt(r['2025H2'])} |")
    lines += ["", f"Kronos 与 LightGBM 预测的平均截面秩相关：{corr:+.3f}", "",
              "## 配对比较与增量（SPEC §9，同日同股票）", "",
              f"- LightGBM − Kronos 的 IC 差：{fmt(paired)}",
              f"- Kronos 对 LightGBM 的残差 IC（逐日截面回归去掉 LightGBM 部分）：{fmt(resid_ic)}"]
    lines += [f"- 混合 (1−w)·LGBM + w·Kronos 相对纯 LGBM 的 IC 增量，w={w}：{fmt(s)}"
              for w, s in blends.items()]
    lines += ["- 注意：混合权重是在测试期上扫的 3 个值，属于探索性分析，不能当作预注册结论", "",
              "## 证据边界",
              "只比较排序能力，不含成本与成交约束；抽样日少，MDE 较宽，差异小于 MDE 时不能下结论。"]
    (RUN / "kronos_report.md").write_text("\n".join(lines), encoding="utf-8")
    (RUN / "kronos_metrics.json").write_text(json.dumps(
        {"model": MODEL, "cfg": cfg, "days": len(K), "rows": rows, "corr_with_lgbm": corr,
         "paired_lgbm_minus_kronos": paired, "kronos_resid_ic": resid_ic,
         "blend_increment": {str(w): s for w, s in blends.items()}},
        indent=2, default=str), encoding="utf-8")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--retry")
    ap.add_argument("--report-only")
    args = ap.parse_args()
    cfg = {"every": args.every, "lookback": args.lookback, "batch": args.batch,
           "max_context": 512, "seed": 0, "model": MODEL}
    store = Store(ROOT / "runs" / "jobs")
    if args.report_only:
        report(args.report_only, store, store.spec(args.report_only)["config"])
        return 0
    if args.retry:
        jid = args.retry
        store.recover(jid)
        store.retry(jid)
        cfg = store.spec(jid)["config"]
    else:
        jid = store.submit(JobSpec("evaluate", cfg, resources="gpu"))
    print("job", jid, flush=True)
    state = store.run(jid, job)
    print("state", state, store.status(jid).get("error") or "", flush=True)
    if state != "succeeded":
        return 1
    report(jid, store, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
