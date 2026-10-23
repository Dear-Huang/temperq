"""Independent fractional total-return-unit ledger, not a broker simulator."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import rules


DEFAULT_ACCOUNT = {
    "model": "weight_research_v1", "initial_cash": 1_000_000.0,
    "top_share": 0.1, "commission_bp": 3.0,
    "spread_model": "struct_previous_month_v1", "extra_slippage_bp": [0.0, 10.0, 25.0],
    "history_eligibility": "all_three_rule_scores_finite_at_decision",
    "execution": "next_open_daily_bar_proxy", "mark_missing": "carry_last_adjusted_mark_flagged",
    "corporate_actions": "fractional_adjusted_units_total_return_proxy",
    "terminal": "mark_to_close_no_forced_liquidation", "annual_days": 252,
    "min_net_excess_ann_bp": 0.0,
}


def replay(panel, normal, spread_bp, targets, *, start, end, initial_cash,
           commission_bp, extra_slippage_bp=0.0, explained=None):
    """targets maps decision indices to weights; sells precede pro-rata funded buys.

    Units are raw shares / adj_factor. A factor change keeps units constant,
    approximating reinvested corporate actions; no cash dividend ledger is claimed.
    Missing marks use only the last observed adjusted mark. `explained` (same
    shape as the panel) marks days with a lifecycle record (suspension or after
    delisting); only unexplained stale marks and data gaps disable qualification.
    """
    dates, codes = panel["close"].index, panel["close"].columns
    op, cl, adj, up, dn = (panel[k].to_numpy() for k in
                           ("open", "close", "adj_factor", "up_limit", "down_limit"))
    normal, spread = np.asarray(normal), np.asarray(spread_bp, dtype=float)
    n = len(codes)
    if not 0 <= start < end < len(dates) or initial_cash <= 0:
        raise ValueError("invalid account window or cash")
    if not np.isfinite([initial_cash, commission_bp, extra_slippage_bp]).all() or min(commission_bp, extra_slippage_bp) < 0:
        raise ValueError("invalid account costs")
    explained = np.zeros(cl.shape, bool) if explained is None else np.asarray(explained, bool)
    if normal.shape != cl.shape or spread.shape != cl.shape or explained.shape != cl.shape:
        raise ValueError("execution arrays are not aligned")
    for t, w in targets.items():
        w = np.asarray(w, dtype=float)
        if (type(t) is not int or not start <= t < end or w.shape != (n,)
                or not np.isfinite(w).all() or (w < 0).any() or w.sum() > 1 + 1e-12):
            raise ValueError("invalid long-only target")
    units, last = np.zeros(n), np.full(n, np.nan)
    cash, previous = float(initial_cash), float(initial_cash)
    days, fills, holdings = [], [], []
    for t in range(start, end + 1):
        factor_ok = np.isfinite(adj[t]) & (adj[t] > 0)
        open_ok = np.isfinite(op[t]) & (op[t] > 0) & factor_ok
        close_ok = np.isfinite(cl[t]) & (cl[t] > 0) & factor_ok
        ao = np.where(open_ok, op[t] * adj[t], np.nan)
        ac = np.where(close_ok, cl[t] * adj[t], np.nan)
        open_mark = np.where(open_ok, ao, last)
        cost = commission = stamp = slip = notional = 0.0
        requested, rejected, scaled, data_gap_orders = 0, 0, 0, 0
        stale_open = unexplained_open = unexplained_gaps = 0
        if t - 1 in targets:
            w = np.asarray(targets[t - 1], dtype=float)
            held = units > 0
            stale_open = int((held & ~open_ok).sum())
            unexplained_open = int((held & ~open_ok & ~explained[t]).sum())
            if np.any(held & ~np.isfinite(open_mark)):
                raise ValueError("holding has no historical valuation")
            equity_open = cash + np.dot(units[held], open_mark[held])
            target_units = np.zeros(n)
            valid_target = (w > 0) & open_ok
            target_units[valid_target] = w[valid_target] * equity_open / ao[valid_target]
            # No executable price means existing quantities cannot be resized.
            target_units[~open_ok] = units[~open_ok]
            delta = target_units - units
            wanted = (np.abs(delta) > 1e-12) | (((w > 0) | (units > 0)) & ~open_ok)
            requested = int(wanted.sum())
            bp = spread[t] + extra_slippage_bp
            fee_ok = np.isfinite(bp) & (bp >= 0) & (bp + commission_bp < 10000)
            limits_ok = np.isfinite(up[t]) & np.isfinite(dn[t]) & (dn[t] > 0) & (up[t] > dn[t])
            status_ok = normal[t].astype(bool)
            common = open_ok & status_ok & limits_ok & fee_ok
            buy_ok = common & (op[t] < up[t] - 1e-6)
            sell_ok = common & (op[t] > dn[t] + 1e-6)
            selling = (delta < -1e-12) & sell_ok
            buying = (delta > 1e-12) & buy_ok
            sell_value = np.where(selling, -delta * ao, 0.0)
            sell_value = np.nan_to_num(sell_value)
            tax_bp = rules.stamp_tax_sell_bp(dates[t])
            cash += float(sell_value.sum() - np.sum(sell_value[selling] *
                         (commission_bp + bp[selling] + tax_bp) / 10000))
            units[selling] += delta[selling]
            buy_value = np.nan_to_num(np.where(buying, delta * ao, 0.0))
            required = float(np.sum(buy_value[buying] * (1 + (commission_bp + bp[buying]) / 10000)))
            funding = min(1.0, max(0.0, cash) / required) if required else 1.0
            buy_value *= funding
            units[buying] += delta[buying] * funding
            cash -= float(np.sum(buy_value[buying] * (1 + (commission_bp + bp[buying]) / 10000)))
            for j in np.flatnonzero(wanted):
                side = "BUY" if delta[j] > 0 or (w[j] > 0 and not open_ok[j]) else "SELL"
                value = float(buy_value[j] if side == "BUY" else sell_value[j])
                if not open_ok[j]:
                    reason = "MISSING_OPEN_OR_FACTOR"
                elif not status_ok[j]:
                    reason = "NON_NORMAL_OR_UNKNOWN_STATE"
                elif not limits_ok[j]:
                    reason = "MISSING_LIMITS"
                elif not fee_ok[j]:
                    reason = "MISSING_SPREAD"
                elif side == "BUY" and not buy_ok[j]:
                    reason = "LIMIT_UP"
                elif side == "SELL" and not sell_ok[j]:
                    reason = "LIMIT_DOWN"
                elif side == "BUY" and funding < 1 - 1e-12:
                    reason = "CASH_SCALED"
                else:
                    reason = "FILLED"
                c = value * commission_bp / 10000
                s = value * tax_bp / 10000 if side == "SELL" else 0.0
                b = value * bp[j] / 10000 if value else 0.0
                fee = c + s + b
                commission += c
                stamp += s
                slip += b
                cost += fee
                notional += value
                rejected += int(value == 0)
                scaled += int(reason == "CASH_SCALED")
                data_gap_orders += int(reason.startswith("MISSING_"))
                unexplained_gaps += int(reason.startswith("MISSING_") and not explained[t, j])
                qty = value / ao[j] if value else 0.0
                fills.append((dates[t], dates[t - 1], str(codes[j]), side, reason,
                              qty, float(op[t, j]) if open_ok[j] else None,
                              float(adj[t, j]) if factor_ok[j] else None,
                              value, c, s, b, fee))
        held = units > 0
        last[close_ok] = ac[close_ok]
        if np.any(held & ~np.isfinite(last)):
            raise ValueError("holding has no historical close mark")
        if cash < -initial_cash * 1e-10 or (units < -1e-10).any():
            raise ValueError("self-financing constraint violated")
        cash = max(0.0, cash)
        values = units[held] * last[held]
        equity = float(cash + values.sum())
        days.append((dates[t], equity, cash, float(values.sum()), equity / previous - 1,
                     commission, stamp, slip, cost, notional,
                     notional / previous / 2, int(held.sum()), int((held & ~close_ok).sum()),
                     stale_open, requested, rejected, scaled, data_gap_orders,
                     int((held & ~close_ok & ~explained[t]).sum()), unexplained_open, unexplained_gaps))
        idx = np.flatnonzero(held)
        if len(idx):
            holdings.append(pd.DataFrame({"date": dates[t], "code": codes[idx],
                "units": units[idx].copy(), "adjusted_mark": last[idx],
                "market_value": values, "stale": ~close_ok[idx]}))
        previous = equity
    path = pd.DataFrame(days, columns=["date", "equity", "cash", "holdings_value", "net_return",
        "commission", "stamp_tax", "slippage", "total_cost", "traded_notional", "one_way_turnover",
        "held_names", "stale_close_names", "stale_open_names", "requested_orders", "rejected_orders", "scaled_orders", "data_gap_orders",
        "unexplained_stale_close_names", "unexplained_stale_open_names", "unexplained_data_gap_orders"])
    fill_frame = pd.DataFrame(fills, columns=["date", "decision_date", "code", "side", "reason",
        "units", "raw_open", "adj_factor", "notional", "commission", "stamp_tax", "slippage", "total_cost"])
    for field in ("date", "decision_date"):
        fill_frame[field] = pd.to_datetime(fill_frame[field])
    for field in ("units", "raw_open", "adj_factor", "notional", "commission", "stamp_tax", "slippage", "total_cost"):
        fill_frame[field] = fill_frame[field].astype(float)
    holding_frame = (pd.concat(holdings, ignore_index=True) if holdings else
                     pd.DataFrame(columns=["date", "code", "units", "adjusted_mark", "market_value", "stale"]))
    holding_frame["date"] = pd.to_datetime(holding_frame["date"])
    for field in ("units", "adjusted_mark", "market_value"):
        holding_frame[field] = holding_frame[field].astype(float)
    holding_frame["stale"] = holding_frame["stale"].astype(bool)
    return path, fill_frame, holding_frame


def metrics(path, initial_cash):
    r = path["net_return"].to_numpy()[1:]
    equity = path["equity"].to_numpy()
    if not len(r) or not np.isfinite(r).all() or (equity <= 0).any():
        raise ValueError("invalid daily account path")
    vol = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    peak = np.maximum.accumulate(np.r_[initial_cash, equity])[1:]
    return {"days": len(r), "total_return": float(equity[-1] / initial_cash - 1),
            "cagr": float((equity[-1] / initial_cash) ** (252 / len(r)) - 1),
            "max_drawdown": float(np.min(equity / peak - 1)),
            "annual_volatility": vol * np.sqrt(252),
            "sharpe": float(np.mean(r) / vol * np.sqrt(252)) if vol > 1e-15 else None,
            "sharpe_reason": None if vol > 1e-15 else "zero_volatility_or_insufficient_dates",
            "commission": float(path["commission"].sum()), "stamp_tax": float(path["stamp_tax"].sum()),
            "slippage": float(path["slippage"].sum()), "total_cost": float(path["total_cost"].sum()),
            "mean_one_way_turnover": float(path["one_way_turnover"].iloc[1:].mean()),
            "stale_close_name_days": int(path["stale_close_names"].sum()),
            "stale_open_name_days": int(path["stale_open_names"].sum()),
            "rejected_orders": int(path["rejected_orders"].sum()),
            "requested_orders": int(path["requested_orders"].sum()),
            "scaled_orders": int(path["scaled_orders"].sum()),
            "data_gap_orders": int(path["data_gap_orders"].sum()),
            # v0.3 paths have no lifecycle columns: fall back to raw counts (all unexplained).
            "unexplained_stale_close_name_days": int(path.get("unexplained_stale_close_names", path["stale_close_names"]).sum()),
            "unexplained_stale_open_name_days": int(path.get("unexplained_stale_open_names", path["stale_open_names"]).sum()),
            "unexplained_data_gap_orders": int(path.get("unexplained_data_gap_orders", path["data_gap_orders"]).sum()),
            "ending_cash": float(path["cash"].iloc[-1]), "ending_equity": float(equity[-1]),
            "ending_held_names": int(path["held_names"].iloc[-1])}
