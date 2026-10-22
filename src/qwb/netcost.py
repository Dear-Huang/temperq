"""Net-of-cost check for a ranking signal (guardrail: "IC is not a return").

A deliberately plain long-only test, the same for every signal:
- every h trading days (h = label window), buy the top `top` share of the
  universe by signal, equal weight, at the label's entry price (e.g. open t+1),
  hold to the label's exit price;
- cannot buy a name that is suspended or sits at the up limit at entry;
  cannot sell one that is suspended or at the down limit, so it is carried;
- cost per one-way trade = half the STRUCT quoted spread (ashare-tcost, from
  the previous month's daily features, so no look-ahead) + commission;
  sells also pay stamp duty from the rule pack.
Not modelled: price impact of large orders, intraperiod drift of weights.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import metrics, rules

TRADING_DAYS = 252


def struct_half_spread_bp(P: dict) -> pd.DataFrame:
    """Half of the STRUCT quoted spread (bp) per date x code, using month m-1 inputs.

    Inputs follow ashare-tcost's definitions: mean daily traded value (yuan) and
    mean raw close in the month, last 21-day return volatility in the month.
    """
    import ashare_tcost as tc

    dates = P["close"].index
    month = dates.to_period("M")
    ret = (P["close"] * P["adj_factor"]).pct_change(fill_method=None)
    vol = ret.rolling(21, min_periods=10).std()
    amt_m = P["amount"].where(P["amount"] > 0).groupby(month).mean()
    px_m = P["close"].groupby(month).mean()
    vol_m = vol.groupby(month).last()
    vol_m = vol_m.apply(lambda r: r.fillna(r.median()), axis=1).clip(lower=1e-4)
    long = pd.DataFrame({"amount": amt_m.stack(future_stack=True),
                         "price": px_m.stack(future_stack=True),
                         "vol": vol_m.stack(future_stack=True)})
    spread = tc.struct_spread(long).unstack()
    spread = spread.reindex(index=amt_m.index, columns=P["close"].columns).shift(1)  # month m-1
    out = spread.reindex(month).set_axis(dates) / 2.0
    return out


# Qlib's published LightGBM benchmark (examples/benchmarks/LightGBM/
# workflow_config_lightgbm_Alpha158.yaml): fill at close, one 9.5% limit for every
# board, open_cost 0.0005 / close_cost 0.0015. Its strategy trades on the previous
# step's prediction, so the fill is the next day's close, not the signal day's.
QLIB_STYLE = {"fixed_cost_bp": (5.0, 15.0), "uniform_limit": 0.095}


def labels_close(P: dict, card: dict) -> pd.DataFrame:
    """Same holding window as the card, filled at closes (day+1 close to day+1+h close)."""
    s, e = int(card["label"]["start"]["day"]), int(card["label"]["end"]["day"])
    ac = P["close"] * P["adj_factor"]
    return ac.shift(-e) / ac.shift(-s) - 1


def compare_conventions(P: dict, card: dict, signal: np.ndarray, U: np.ndarray,
                        y_open: np.ndarray, years: list[int], **kw) -> dict:
    """The same signal under qwb's tradable rules and under Qlib's benchmark defaults,
    then one difference at a time, so the gap can be attributed."""
    yc = labels_close(P, card).to_numpy()
    yc = np.where(U, yc, np.nan)
    runs = {
        "qwb 可成交口径": dict(),
        "改为收盘价成交": dict(fill="close", _y=yc),
        "再改为统一 9.5% 涨跌停": dict(fill="close", uniform_limit=0.095, _y=yc),
        "再改为固定费率（Qlib 基准口径）": dict(fill="close", _y=yc, **QLIB_STYLE),
    }
    out = {}
    for name, opt in runs.items():
        y = opt.pop("_y", y_open)
        r = evaluate(P, card, {"s": signal}, U, y, years, **kw, **opt)
        out[name] = r["signals"]["s"]
    return out


def _entry_exit(card: dict) -> tuple[int, int, str, str]:
    s, e = card["label"]["start"], card["label"]["end"]
    return int(s["day"]), int(e["day"]), s.get("price", "open"), e.get("price", "open")


def evaluate(P: dict, card: dict, signals: dict, U: np.ndarray, y_raw: np.ndarray,
             years: list[int], top: float = 0.1, commission_bp: float = 3.0,
             n_hold: int | None = None,
             half_spread: pd.DataFrame | None = None, pack: dict | None = None,
             fixed_cost_bp: tuple[float, float] | None = None,
             fill: str | None = None, uniform_limit: float | None = None) -> dict:
    """fixed_cost_bp=(buy, sell) replaces spread + commission + stamp duty;
    fill="close" fills at the entry day's close instead of the label's entry price;
    uniform_limit=0.095 blocks trades by one rule for every board instead of the
    rule-pack limit prices. All three exist to reproduce other tools' defaults."""
    dates = P["close"].index
    s_off, e_off, s_px, e_px = _entry_exit(card)
    if fill:
        s_px = fill
    h = e_off - s_off
    cand = np.flatnonzero(np.isin(dates.year, years))
    rebal = [t for t in cand[::h] if t + e_off < len(dates)]
    if fixed_cost_bp is None and half_spread is None:
        half_spread = struct_half_spread_bp(P)
    hs = half_spread.to_numpy() if half_spread is not None else None
    px_in = P[s_px].to_numpy()
    if uniform_limit is not None:
        prev = (P["close"] * P["adj_factor"]).shift(1) / P["adj_factor"]
        up = (prev * (1 + uniform_limit)).to_numpy()
        dn = (prev * (1 - uniform_limit)).to_numpy()
    else:
        up = P["up_limit"].to_numpy()
        dn = P["down_limit"].to_numpy() if "down_limit" in P else np.full_like(up, np.nan)
    with np.errstate(invalid="ignore"):
        buyable = ~np.isnan(px_in) & ~(px_in >= up - 1e-6)
        sellable = ~np.isnan(px_in) & ~(px_in <= dn + 1e-6)

    bench = np.array([np.nanmean(np.where(U[t], y_raw[t], np.nan)) for t in rebal])
    out = {"rebalances": len(rebal), "hold_days": h, "periods_per_year": TRADING_DAYS / h,
           "benchmark_ann": float(np.nanmean(bench) * TRADING_DAYS / h), "signals": {}}
    for name, sig in signals.items():
        w0: dict[int, float] = {}
        gross, cost, turn, net_x = [], [], [], []
        for k, t in enumerate(rebal):
            te = t + s_off
            v = U[t] & ~np.isnan(sig[t])
            idx = np.flatnonzero(v)
            n_top = min(n_hold, len(idx)) if n_hold else max(1, int(len(idx) * top))
            target = set(idx[np.argsort(-sig[t, idx])[:n_top]])
            keep = {j for j in w0 if j in target or not sellable[te, j]}
            new = {j for j in target - set(w0) if buyable[te, j]}
            hold = sorted(keep | new)
            if not hold:                 # nothing tradable: stay in cash for the period
                gross.append(0.0)
                cost.append(0.0)
                turn.append(0.0)
                net_x.append(np.nan)
                w0 = {}
                continue
            w1 = {j: 1.0 / len(hold) for j in hold}
            names = set(w0) | set(w1)
            if hs is not None:
                med = np.nanmedian(hs[te]) if np.isfinite(hs[te]).any() else 0.0
                stamp = rules.stamp_tax_sell_bp(dates[te], pack)
            c_bp, tv = 0.0, 0.0
            for j in names:
                dw = w1.get(j, 0.0) - w0.get(j, 0.0)
                if dw == 0:
                    continue
                if fixed_cost_bp is not None:
                    c_bp += dw * fixed_cost_bp[0] if dw > 0 else -dw * fixed_cost_bp[1]
                else:
                    hsj = hs[te, j] if np.isfinite(hs[te, j]) else med
                    c_bp += abs(dw) * (hsj + commission_bp) + (stamp * -dw if dw < 0 else 0.0)
                tv += abs(dw)
            r = np.array([y_raw[t, j] for j in hold])
            g = float(np.nansum(np.nan_to_num(r)) / len(hold))
            gross.append(g)
            cost.append(c_bp / 1e4)
            turn.append(tv / 2)          # one-way
            net_x.append(g - c_bp / 1e4 - bench[k])
            w0 = w1
        gross, cost, net_x = np.array(gross), np.array(cost), np.array(net_x)
        ppy = TRADING_DAYS / h
        nw = metrics.newey_west(net_x, 1)
        g_ann = float(np.mean(gross) * ppy)
        out["signals"][name] = {
            "gross_ann": g_ann,
            "gross_excess_ann": float(np.nanmean(gross - bench) * ppy),
            "net_excess_ann": float(np.nanmean(net_x) * ppy),
            "net_excess_t": nw["t"],
            "cost_ann": float(np.mean(cost) * ppy),
            "turnover_one_way": float(np.mean(turn)),
            "cost_share_of_gross_excess": (float(np.mean(cost) / np.nanmean(gross - bench))
                                           if np.nanmean(gross - bench) > 0 else np.nan),
        }
    return out
