"""Can this capital hold these target weights at all? An integer lower bound on the allocation gap.

For a target weight vector w, prices p, lot sizes l and capital A, the optimistic distance

    L(A) = min  sum_i |l p_i n_i / A - w_i|  +  |cash share - target cash share|
           s.t. n_i >= 0 integer,  sum_i l p_i n_i <= A

ignores fees, T+1, volume limits, timing and the path from the current holdings, so the feasible set is
wider than any real account's. That makes L a floor: if even L exceeds the tolerance, no execution of this
strategy at this capital can meet it. A small L proves nothing; it only fails to rule the target out.
L is measured on both the asset and the cash side, so it runs from 0 to 200 percentage points.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

LOT = 100
GRID = (10_000., 30_000., 100_000., 300_000., 1_000_000.)


def floor(weights, prices, capital, lot=LOT):
    """Smallest L1 distance an integer-lot portfolio can reach. weights/prices are aligned lists."""
    from scipy.optimize import Bounds, LinearConstraint, milp
    k = len(weights)
    unit = np.array([lot * p / capital for p in prices])             # weight of one lot
    target_cash = max(0., 1. - sum(weights))
    # variables: n (k, integer), e (k, abs gap per name), c (1, abs gap of cash)
    nvar = 2 * k + 1
    cost = np.r_[np.zeros(k), np.ones(k), 1.]
    rows, lo, hi = [], [], []
    for i in range(k):
        a = np.zeros(nvar); a[i] = unit[i]; a[k + i] = -1.            # unit n - e <= w
        rows.append(a); lo.append(-np.inf); hi.append(weights[i])
        b = np.zeros(nvar); b[i] = -unit[i]; b[k + i] = -1.           # -unit n - e <= -w
        rows.append(b); lo.append(-np.inf); hi.append(-weights[i])
    cash = np.zeros(nvar); cash[:k] = unit                            # invested share <= 1
    rows.append(cash); lo.append(-np.inf); hi.append(1.)
    # cash share = 1 - invested; |1 - invested - target_cash| <= c
    c1 = np.zeros(nvar); c1[:k] = -unit; c1[-1] = -1.
    rows.append(c1); lo.append(-np.inf); hi.append(target_cash - 1.)
    c2 = np.zeros(nvar); c2[:k] = unit; c2[-1] = -1.
    rows.append(c2); lo.append(-np.inf); hi.append(1. - target_cash)
    top = [np.floor(1. / u) if u > 0 else 0. for u in unit]
    result = milp(cost, integrality=np.r_[np.ones(k), np.zeros(k + 1)],
                  bounds=Bounds(np.zeros(nvar), np.r_[top, np.full(k + 1, np.inf)]),
                  constraints=LinearConstraint(np.array(rows), lo, hi))
    if not result.success:
        return None
    return float(result.fun)


def analyze(bundle, accounting, thresholds=None):
    targets = bundle.get("target_weights") or []
    if not targets or accounting.get("status") != "verified":
        return None
    eps = (thresholds or {}).get("T-CAPITAL-L1", .2)
    plan = defaultdict(dict)
    for t in targets:
        plan[t["date"]][t["code"]] = t["target_weight"]
    known = defaultdict(dict)                                          # last seen price per code
    for p in bundle.get("positions") or []:
        if p.get("price"):
            known[p["date"]][p["code"]] = p["price"]
    for f in bundle.get("fills") or []:
        known[f["date"]].setdefault(f["code"], f["price"])
    for p in bundle.get("prices") or []:
        known[p["date"]].setdefault(p["code"], p["close"])
    equity = {s["date"]: s["equity"] for s in bundle.get("snapshots") or []}
    last, priced = {}, {}
    for d in sorted(set(known) | set(plan)):
        last.update(known.get(d, {}))
        if d in plan:
            priced[d] = dict(last)
    rows, months, at_account = [], set(), []
    for d in sorted(plan):
        codes = [c for c in plan[d] if plan[d][c] > 0 and priced[d].get(c)]
        if len(codes) < len([c for c in plan[d] if plan[d][c] > 0]):
            continue                                                   # a target name with no price yet
        w = [plan[d][c] for c in codes]
        p = [priced[d][c] for c in codes]
        if d in equity and equity[d] > 0:
            v = floor(w, p, equity[d])
            if v is not None:
                at_account.append(v)
        if d[:7] not in months:                                        # the grid on the first plan date of each month
            months.add(d[:7])
            rows.append({"date": d, **{str(int(a)): floor(w, p, a) for a in GRID}})
    if not rows:
        return None
    grid = []
    for a in GRID:
        vals = [r[str(int(a))] for r in rows if r[str(int(a))] is not None]
        if vals:
            grid.append({"capital": a, "mean": sum(vals) / len(vals), "worst": max(vals),
                         "infeasible_share": sum(v > eps for v in vals) / len(vals)})
    out = {"id": "capital", "tolerance": eps, "grid": grid, "dates": len(rows),
           "account_mean": sum(at_account) / len(at_account) if at_account else None,
           "scope": "整数手数下的乐观下界：忽略费用、T+1、成交量、时点和调仓路径；"
                    "下界超过容忍度可证明做不到，下界小不证明做得到"}
    blocked = [g for g in grid if g["infeasible_share"] > .5]
    out["status"] = "limited" if blocked else "open"
    acct = (f"按账户实际权益逐日计算，乐观下界平均 {out['account_mean']:.1%}（L1，资产和现金两侧合计）。"
            if out["account_mean"] is not None else "")
    out["verdict"] = (acct + (f"资金 {blocked[-1]['capital']:,.0f} 元及以下时，多数调仓日连乐观下界都超过 {eps:.0%}："
                              "这么小的账户按整手买不出这组目标权重" if blocked else
                              f"列出的资金规模下，乐观下界都没有超过 {eps:.0%}；这不证明实际能兑现"))
    return out
