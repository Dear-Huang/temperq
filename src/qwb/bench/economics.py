"""Common execution environment for signals and fixed economic references."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .. import netcost
from . import account
from .statistics import block_summary, conclusion


LIMITS = [
    "分数复权单位近似总回报再投资，不是原始股数、现金分红与公司行动逐笔账本。",
    "开盘价和当日状态仅为日线成交代理，无法验证竞价队列、开盘深度或实际成交概率。",
    "STRUCT 价差为上月输入估计；佣金假设包含经手等费用，不含最低佣金；另按日期收卖出印花税。",
    "未模拟整数手、冲击、资金容量、订单延迟、停牌后复牌队列、税费个体差异或退市清算。",
    "缺失持仓估值仅沿用过去复权价格并标记；任何陈旧估值会使结果仅供诊断，不获正式经济资格。",
    "期末按收盘估值，不强制卖出，不收取假想平仓费用；余额不是可立即提取的现金。",
    "经济回放不依赖未来标签可用性；与 IC 样本/日期不同，不能把两种结果混算。",
    "历史开发诊断，不包含完整搜索校正、重复训练、独立前向或风险预算准入。",
]


def load_normal(root, dates, codes):
    files = sorted((root / "bars").glob("*.parquet"))
    if any("state" not in pq.read_schema(f).names for f in files):
        raise ValueError("state field required; missing status cannot be assumed normal")
    status = pd.concat([pd.read_parquet(f, columns=["date", "code", "state"]) for f in files])
    status["date"] = pd.to_datetime(status["date"])
    return status.pivot(index="date", columns="code", values="state").reindex(
        index=dates, columns=codes).eq("normal").to_numpy()


def targets_for(scores, universe, decisions, share):
    out = {}
    for t in decisions:
        idx = np.flatnonzero(universe[t])
        if not len(idx):
            raise ValueError(f"empty decision universe at index {t}")
        if scores is not None:
            if not np.isfinite(scores[t, idx]).all():
                raise ValueError(f"incomplete decision predictions at index {t}")
            idx = idx[np.argsort(-scores[t, idx], kind="stable")[:max(1, int(len(idx) * share))]]
        weights = np.zeros(universe.shape[1])
        weights[idx] = 1 / len(idx)
        out[int(t)] = weights
    return out


def summarize(paths, protocol):
    """Recompute metrics and paired excess from scorer-produced account paths."""
    cfg = protocol["account"]
    groups, summary = {}, {}
    for (scenario, name), frame in paths.groupby(["scenario", "strategy"], sort=False):
        frame = frame.sort_values("date").reset_index(drop=True)
        groups[(scenario, name)] = frame
        summary.setdefault(scenario, {})[name] = account.metrics(frame, cfg["initial_cash"])
    for (scenario, name), frame in groups.items():
        base = groups[(scenario, "equal_weight")]
        if not frame["date"].equals(base["date"]):
            raise ValueError("economic comparison date mismatch")
        diff = (frame["net_return"] - base["net_return"]).to_numpy()[1:]
        stat = block_summary(diff, **protocol["statistics"])[0]
        entry = summary[scenario][name]
        entry["net_excess_ann_bp"] = stat["mean"] * 252 * 10000
        entry["net_excess_ann_ci95_bp"] = None if stat["ci95"] is None else [x * 252 * 10000 for x in stat["ci95"]]
        entry["net_excess_conclusion"] = conclusion(stat, cfg["min_net_excess_ann_bp"] / (252 * 10000))
        entry["qualification"] = ("DIAGNOSTIC_ONLY" if any(summary[scenario][x][k]
            for x in (name, "equal_weight") for k in ("stale_close_name_days", "stale_open_name_days", "data_gap_orders"))
            else "CONDITIONAL_RESEARCH")
        entry["by_year"] = {}
        for year in sorted(set(frame["date"].dt.year)):
            rows = frame["date"].dt.year.to_numpy()[1:] == year
            r = frame["net_return"].to_numpy()[1:][rows]
            entry["by_year"][str(year)] = {"days": int(rows.sum()),
                "net_return": float(np.prod(1 + r) - 1),
                "net_excess_ann_bp": float(np.mean(diff[rows]) * 252 * 10000)}
    return summary


def evaluate(panel, root, universe, predictions, references, protocol):
    cfg = protocol["account"]
    dates, codes = panel["close"].index, panel["close"].columns
    window = np.flatnonzero(np.isin(dates.year, protocol["evaluation_years"]))
    if len(window) < 2:
        raise ValueError("insufficient economic dates")
    normal = load_normal(root, dates, codes)
    if "down_limit" not in panel:
        raise ValueError("down_limit field required for account replay")
    spread = netcost.struct_half_spread_bp(panel).to_numpy()
    h = protocol["task"]["target"]["horizon_days"]
    decisions = window[:-1:h]
    common = universe.copy()
    for name in protocol["references"][:-1]:
        common &= np.isfinite(references[name])
    eligibility = {"decision_raw_universe_pairs": int(universe[decisions].sum()),
                   "decision_eligible_pairs": int(common[decisions].sum()),
                   "excluded_missing_past_reference_pairs": int((universe[decisions] & ~common[decisions]).sum()),
                   "uses_future_labels": False}
    signals = {"candidate": predictions, **references, "equal_weight": None}
    target_tables = {}
    for name, s in signals.items():
        try:
            target_tables[name] = targets_for(s, common, decisions, 1.0 if name == "equal_weight" else cfg["top_share"])
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    target_tables["cash"] = {}
    paths, fills, holdings = [], [], []
    for extra in cfg["extra_slippage_bp"]:
        scenario = f"extra_{extra:g}bp_per_side"
        names = target_tables if extra == 0 else ("candidate", "equal_weight")
        for name in names:
            p, f, held = account.replay(panel, normal, spread, target_tables[name],
                start=int(window[0]), end=int(window[-1]), initial_cash=cfg["initial_cash"],
                commission_bp=cfg["commission_bp"], extra_slippage_bp=extra)
            for frame, container in ((p, paths), (f, fills), (held, holdings)):
                frame["scenario"], frame["strategy"] = scenario, name
                container.append(frame)
    paths = pd.concat(paths, ignore_index=True)
    fills = pd.concat(fills, ignore_index=True)
    holdings = pd.concat(holdings, ignore_index=True)
    summary = summarize(paths, protocol)
    result = {"status": "COMPLETED", "scope": "development_conditional_fractional_research",
              "formal_score_eligible": False, "account_config": cfg,
              "decision_dates": len(decisions), "start": str(dates[window[0]].date()),
              "universe_eligibility": eligibility,
              "end": str(dates[window[-1]].date()), "results": summary, "limits": LIMITS,
              "score_unit": "252 * 10000 * mean(daily_net_return - realizable_equal_weight_daily_net_return)",
              "interval_scope": "marginal_date_block_only_not_search_or_execution_uncertainty"}
    return result, paths, fills, holdings


def verify_ledger(paths, fills, holdings, protocol):
    """Audit cash flows, quantities and close marks independently of replay's loop."""
    if paths.duplicated(["scenario", "strategy", "date"]).any():
        raise ValueError("duplicate account date")
    cash0 = protocol["account"]["initial_cash"]
    for (scenario, name), p in paths.groupby(["scenario", "strategy"], sort=False):
        p = p.sort_values("date").reset_index(drop=True)
        f = fills[(fills["scenario"] == scenario) & (fills["strategy"] == name)].copy()
        h = holdings[(holdings["scenario"] == scenario) & (holdings["strategy"] == name)].copy()
        if not set(f["side"]) <= {"BUY", "SELL"} or (f[["units", "notional", "commission", "stamp_tax", "slippage", "total_cost"]] < 0).any().any():
            raise ValueError("invalid signed fill or fees")
        if not f.empty:
            if (f["decision_date"] >= f["date"]).any() or (f["units"] < 0).any():
                raise ValueError("invalid execution time or quantity")
            if not np.allclose(f["notional"], f["units"] * f["raw_open"].fillna(0) * f["adj_factor"].fillna(0), rtol=0, atol=1e-7):
                raise ValueError("fill notional does not reconcile")
            if not np.allclose(f["total_cost"], f[["commission", "stamp_tax", "slippage"]].sum(axis=1), rtol=0, atol=1e-7):
                raise ValueError("fill cost does not reconcile")
        f["signed_units"] = np.where(f["side"] == "BUY", f["units"], -f["units"])
        f["cash_flow"] = np.where(f["side"] == "BUY", -f["notional"], f["notional"]) - f["total_cost"]
        flows = f.groupby("date")["cash_flow"].sum().reindex(p["date"], fill_value=0).to_numpy(dtype=float)
        if not np.allclose(p["cash"], cash0 + np.cumsum(flows), rtol=0, atol=cash0 * 1e-9):
            raise ValueError("cash ledger does not reconcile")
        if not h.empty:
            if h.duplicated(["date", "code"]).any() or (h["units"] <= 0).any():
                raise ValueError("invalid holding quantities")
            if not np.allclose(h["market_value"], h["units"] * h["adjusted_mark"], rtol=0, atol=1e-7):
                raise ValueError("holding valuation does not reconcile")
        values = h.groupby("date")["market_value"].sum().reindex(p["date"], fill_value=0).to_numpy(dtype=float)
        if not np.allclose(p["equity"], p["cash"] + values, rtol=0, atol=cash0 * 1e-9):
            raise ValueError("equity ledger does not reconcile")
        if not np.allclose(p["holdings_value"], values, rtol=0, atol=cash0 * 1e-9):
            raise ValueError("reported holdings value does not reconcile")
        expected_r = p["equity"].to_numpy() / np.r_[cash0, p["equity"].to_numpy()[:-1]] - 1
        if not np.allclose(p["net_return"], expected_r, rtol=0, atol=1e-12):
            raise ValueError("account daily returns do not reconcile")
        if (p["cash"] < -cash0 * 1e-10).any():
            raise ValueError("negative cash")
        # Cumulative fills must reproduce every dated holding, including locked ones.
        if not f.empty or not h.empty:
            movement = f.pivot_table(index="date", columns="code", values="signed_units", aggfunc="sum", fill_value=0)
            movement = movement.reindex(index=p["date"], fill_value=0).cumsum()
            actual = h.pivot(index="date", columns="code", values="units").reindex(
                index=p["date"], columns=movement.columns).fillna(0)
            if set(h["code"]) - set(movement.columns) or not np.allclose(movement, actual, rtol=0, atol=1e-8):
                raise ValueError("holding units do not reconcile with fills")
        for col in ("commission", "stamp_tax", "slippage", "total_cost"):
            observed = f.groupby("date")[col].sum().reindex(p["date"], fill_value=0).to_numpy(dtype=float)
            if not np.allclose(p[col], observed, rtol=0, atol=cash0 * 1e-10):
                raise ValueError("daily costs do not reconcile with fills")
        notional = f.groupby("date")["notional"].sum().reindex(p["date"], fill_value=0).to_numpy(dtype=float)
        turnover = notional / np.r_[cash0, p["equity"].to_numpy()[:-1]] / 2
        if not np.allclose(p["traded_notional"], notional, rtol=0, atol=1e-7) or not np.allclose(p["one_way_turnover"], turnover, rtol=0, atol=1e-12):
            raise ValueError("turnover does not reconcile with fills")
        for col, mask in (("requested_orders", np.ones(len(f), dtype=bool)),
                          ("rejected_orders", f["notional"] == 0),
                          ("scaled_orders", f["reason"] == "CASH_SCALED"),
                          ("data_gap_orders", f["reason"].str.startswith("MISSING_"))):
            counts = f.loc[mask].groupby("date").size().reindex(p["date"], fill_value=0)
            if not np.array_equal(p[col], counts):
                raise ValueError("order counts do not reconcile")
        stale = h.loc[h["stale"]].groupby("date").size().reindex(p["date"], fill_value=0)
        if not np.array_equal(p["stale_close_names"], stale):
            raise ValueError("stale close counts do not reconcile")
