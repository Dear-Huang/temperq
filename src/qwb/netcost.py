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


def _entry_exit(card: dict) -> tuple[int, int, str, str]:
    s, e = card["label"]["start"], card["label"]["end"]
    return int(s["day"]), int(e["day"]), s.get("price", "open"), e.get("price", "open")


def evaluate(P: dict, card: dict, signals: dict, U: np.ndarray, y_raw: np.ndarray,
             years: list[int], top: float = 0.1, commission_bp: float = 3.0,
             half_spread: pd.DataFrame | None = None, pack: dict | None = None) -> dict:
    dates = P["close"].index
    s_off, e_off, s_px, e_px = _entry_exit(card)
    h = e_off - s_off
    cand = np.flatnonzero(np.isin(dates.year, years))
    rebal = [t for t in cand[::h] if t + e_off < len(dates)]
    if half_spread is None:
        half_spread = struct_half_spread_bp(P)
    hs = half_spread.to_numpy()
    px_in = P[s_px].to_numpy()
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
            n_top = max(1, int(len(idx) * top))
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
            med = np.nanmedian(hs[te]) if np.isfinite(hs[te]).any() else 0.0
            stamp = rules.stamp_tax_sell_bp(dates[te], pack)
            c_bp, tv = 0.0, 0.0
            for j in names:
                dw = w1.get(j, 0.0) - w0.get(j, 0.0)
                if dw == 0:
                    continue
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
