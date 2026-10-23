"""Reprice the same realized fills to identify proxy model errors, not alpha."""

from __future__ import annotations

import math


def cost_decomposition(account, allocation, *, schema_version="same_fill_cost_decomposition_v1"):
    if schema_version not in {"same_fill_cost_decomposition_v1", "same_fill_cost_decomposition_v2"}:
        raise ValueError("unsupported same-fill cost decomposition version")
    detailed = schema_version == "same_fill_cost_decomposition_v2"
    if detailed and not account["audits"]:
        raise ValueError("detailed cost decomposition requires nonempty audit coverage")
    initial_equity = float(account["audits"][0]["equity_before"]) if detailed else None
    if detailed and (not math.isfinite(initial_equity) or initial_equity <= 0):
        raise ValueError("cost basis points require positive finite initial equity")
    attempts = {row["decision_id"]: row for row in allocation["attempts"]}
    if len(attempts) != len(allocation["attempts"]) or set(attempts) != {row["decision_id"] for row in account["audits"]}:
        raise ValueError("cost decomposition requires identical unique decision coverage")
    rows, fills = [], []
    for actual in account["audits"]:
        attempt = attempts[actual["decision_id"]]
        components = {key: 0.0 for key in ("minimum_commission_premium", "sell_tax_rate_gap",
                                          "slippage_gap", "negative_proxy_impact")}
        filled_proxy = 0.0
        if attempt["status"] == "target":
            recipe = attempt["recipe"]
            index = {code: i for i, code in enumerate(attempt["assets"])}
            for fill in actual["fills"]:
                value = fill["notional"]
                declared_commission = value * recipe["fees"]["commission_bp"] / 10000
                declared_slippage = value * recipe["fees"]["slippage_bp"] / 10000
                declared_tax = value * recipe["cost"]["sell_tax_bp"] / 10000 if fill["side"] == "SELL" else 0.0
                modeled = value * recipe["buy_cost" if fill["side"] == "BUY" else "sell_cost"][index[fill["asset_id"]]]
                filled_proxy += modeled
                parts = {"minimum_commission_premium": fill["commission"] - declared_commission,
                         "sell_tax_rate_gap": fill["stamp_tax"] - declared_tax,
                         "slippage_gap": fill["slippage"] - declared_slippage,
                         "negative_proxy_impact": -(modeled - declared_commission - declared_slippage - declared_tax)}
                for key, component in parts.items():
                    components[key] += component
                if detailed:
                    actual_cost = fill["total_cost"]
                    gap = actual_cost - modeled
                    residual = gap - sum(parts.values())
                    if (not math.isfinite(residual) or value < 0
                            or abs(residual) > 1e-8 * max(1, actual_cost)):
                        raise ValueError("per-fill cost component reconciliation failed")
                    amounts = {"actual_cost": actual_cost, "filled_proxy_cost": modeled,
                               "actual_to_filled_gap": gap, **parts, "accounting_residual": residual}
                    fills.append({"decision_id": actual["decision_id"], "asset_id": fill["asset_id"],
                        "side": fill["side"], "filled_quantity": fill["quantity"], "notional_cny": value,
                        "cny": amounts,
                        "bp_of_initial_equity": {key: amount / initial_equity * 10000 for key, amount in amounts.items()},
                        "bp_of_fill_notional": {key: amount / value * 10000 if value else None for key, amount in amounts.items()},
                        "relative_actual_to_filled_gap": gap / modeled if modeled > 0 else None})
            intended = attempt["intended_proxy_cost"]
        elif attempt["status"] == "explicit_failure" and actual["total_cost"] == 0:
            intended = 0.0
        else:
            raise ValueError("unrecognized decision status or trading on a failed action")
        residual = actual["total_cost"] - filled_proxy - sum(components.values())
        if not math.isfinite(residual) or abs(residual) > 1e-8 * max(1, actual["total_cost"]):
            raise ValueError("cost component reconciliation failed")
        rows.append({"decision_id": actual["decision_id"], "actual_cost": actual["total_cost"],
            "filled_proxy_cost": filled_proxy, "intended_proxy_cost": intended,
            "intent_to_fill_gap": filled_proxy - intended, **components, "accounting_residual": residual})
    totals = {key: sum(row[key] for row in rows) for key in rows[0] if key != "decision_id"} if rows else {}
    if totals:
        totals["relative_actual_to_filled_gap"] = (totals["actual_cost"] / totals["filled_proxy_cost"] - 1
                                                   if totals["filled_proxy_cost"] > 0 else None)
    result = {"schema_version": schema_version, "daily": rows, "totals": totals,
              "scope": "same realized fills; component identity, not a calibrated cost model or causal performance attribution"}
    if detailed:
        for row in rows:
            row["bp_of_initial_equity"] = {key: value / initial_equity * 10000
                for key, value in row.items() if key != "decision_id"}
            row["relative_actual_to_filled_gap"] = ((row["actual_cost"] - row["filled_proxy_cost"]) / row["filled_proxy_cost"]
                                                   if row["filled_proxy_cost"] > 0 else None)
        result.update(fills=fills, initial_equity_cny=initial_equity,
            totals_bp_of_initial_equity={key: value / initial_equity * 10000
                for key, value in totals.items() if key != "relative_actual_to_filled_gap"},
            denominators={"bp_of_initial_equity": "first audit equity_before, fixed for all decisions and fills",
                          "bp_of_fill_notional": "each individual realized fill notional; zero gives null",
                          "relative_actual_to_filled_gap": "filled proxy cost; zero gives null; absolute CNY gap retained"})
    return result
