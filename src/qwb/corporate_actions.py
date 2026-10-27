"""Recover corporate actions that vendor exports leave implicit.

JoinQuant and RiceQuant exports carry no dividend or bonus-share records; they show up as
an unexplained rise in cash or in share count on a day with no matching trade. This module
only proposes an action where the account itself pins it down:

- cash dividend: cash rises with no fills that day, every position is unchanged, and the gap
  is positive. The amount is the whole cash gap; it is split across names in proportion to
  prior market value only as a labelled estimate.
- bonus or conversion shares: a held name's share count rises with no fill, and the closing
  price falls in proportion (ratio within 3%), so the change matches a split.

Every proposal is marked ``inferred`` and shown to the user to confirm. Days that fit neither
rule stay as reconciliation exceptions.
"""
from __future__ import annotations

from collections import defaultdict


CASH_TOLERANCE = .05


def infer(bundle):
    snaps = {s["date"]: s for s in bundle.get("snapshots", [])}
    if len(snaps) < 2 or not bundle.get("positions"):
        return []
    pos = defaultdict(dict)
    for p in bundle["positions"]:
        pos[p["date"]][p["code"]] = p
    qty, flow, fee = (defaultdict(lambda: defaultdict(float)) for _ in range(3))
    for f in bundle.get("fills", []):
        sign = 1 if f["side"] == "buy" else -1
        qty[f["date"]][f["code"]] += sign * f["quantity"]
        flow[f["date"]][f["code"]] += sign * f["notional"]
        fee[f["date"]][f["code"]] += f["fee"]
    known = {(a["date"], a["code"]) for a in bundle.get("actions", [])}
    days = sorted(snaps)
    out = []
    for prev, day in zip(days, days[1:]):
        before, after = pos[prev], pos[day]
        share_changes = {}
        for code in set(before) | set(after):
            change = after.get(code, {}).get("quantity", 0) - before.get(code, {}).get("quantity", 0) - qty[day][code]
            if change:
                share_changes[code] = change
        cash_gap = snaps[day]["cash"] - (snaps[prev]["cash"] - sum(flow[day].values()) - sum(fee[day].values()))

        # Bonus / conversion shares: price falls in proportion to the extra shares.
        split_codes = []
        for code, change in share_changes.items():
            old, new = before.get(code), after.get(code)
            if (day, code) in known or not old or change <= 0:
                continue
            if new and not qty[day][code]:
                # Held through the ex-date with no trade.
                ratio, price_after, how = new["quantity"] / old["quantity"], new["price"], f"无成交，持股 {old['quantity']}→{new['quantity']}，收盘价 {old['price']}→{new['price']}"
            elif not new and qty[day][code] < 0:
                # Sold out on the ex-date: more shares sold than were held the day before.
                sold = [f for f in bundle["fills"] if f["date"] == day and f["code"] == code and f["side"] == "sell"]
                if not sold:
                    continue
                ratio = sum(f["quantity"] for f in sold) / old["quantity"]
                price_after = min(f["price"] for f in sold)
                how = f"除权日清仓：前一日持 {old['quantity']} 股，当日卖出 {sum(f['quantity'] for f in sold)} 股，成交价 {price_after}，前收 {old['price']}"
            else:
                continue
            # A-share ratios are whole multiples of 0.5 share per 10 (10送3 → 1.3, 10转2.5 → 1.25).
            # The ex-date price also moves with the market, so only require that it fell.
            steps = round((ratio - 1) * 20)
            clean_ratio = steps >= 1 and abs(ratio - (1 + steps / 20)) <= .002
            # Odd ratios (e.g. 10转3.1) are accepted only when the price fell almost exactly in proportion.
            proportional = price_after > 0 and abs(ratio / (old["price"] / price_after) - 1) <= .02
            if (clean_ratio or proportional) and ratio > 1 and price_after < old["price"]:
                split_codes.append(code)
                out.append({"date": day, "code": code, "quantity_change": int(change), "cash": 0.,
                            "kind": "bonus_shares", "inferred": True, "evidence": how})

        # Cash dividend: a positive cash gap that no trade explains. The split shares above are
        # allowed on the same day (stocks often pay both); any other share break blocks it.
        unexplained = [c for c in share_changes if c not in split_codes]
        if cash_gap > CASH_TOLERANCE and not unexplained:
            holders = {c: p["market_value"] for c, p in before.items() if c in after and (day, c) not in known}
            total = sum(holders.values())
            if total > 0:
                for code, value in holders.items():
                    out.append({"date": day, "code": code, "quantity_change": 0,
                                "cash": cash_gap * value / total, "kind": "cash_dividend", "inferred": True,
                                "allocation": "pro_rata_prior_market_value",
                                "evidence": f"当日无对应成交，现金比预期多 {cash_gap:.2f} 元"})
    return out


def apply(bundle, actions):
    """Return a copy with inferred actions added and labelled; the original stays untouched."""
    if not actions:
        return bundle
    out = {**bundle, "actions": list(bundle.get("actions", [])) + [{**a, "ref": None} for a in actions]}
    out["actions"].sort(key=lambda a: (a["date"], a["code"]))
    quality = dict(out.get("quality", {}))
    quality["inferred_actions"] = {"count": len(actions),
                                   "cash_dividend": sum(a["kind"] == "cash_dividend" for a in actions),
                                   "bonus_shares": sum(a["kind"] == "bonus_shares" for a in actions)}
    out["quality"] = quality
    return out
