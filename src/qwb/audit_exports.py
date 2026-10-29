"""Evidence tables an audit result can be exported as; shared by the web page and `qwb audit`."""
from __future__ import annotations

# kind -> (module that must exist, how to get the rows, columns)
TABLES = {
    "stocks": ("attribution", lambda m: m["attribution"]["stocks"],
               ["code", "gross_pnl", "fees", "net_pnl", "initial_capital_bp"]),
    "daily": ("accounting", lambda m: m["accounting"]["contributions"],
              ["date", "code", "gross_pnl", "fees", "net_pnl"]),
    "fees": ("fee_analysis", lambda m: m["fee_analysis"]["fills"],
             ["date", "code", "side", "order_id", "fill_id", "charge_basis", "charge_confirmed", "quantity", "price",
              "notional", "fee", "modeled_commission", "modeled_stamp_tax", "modeled_fee", "reported_minus_modeled"]),
    "fee-units": ("fee_analysis", lambda m: m["fee_analysis"].get("charge_units"),
                  ["date", "code", "side", "order_id", "basis", "declared_basis", "status", "evidence", "fill_count",
                   "notional", "reported_fee", "modeled_fee", "reported_minus_modeled"]),
    "risk": ("risk_analysis", lambda m: m["risk_analysis"]["daily"],
             ["date", "equity", "cash_weight", "invested_weight", "equity_hhi", "invested_hhi", "effective_names",
              "max_weight", "holdings_count"]),
    "tick-costs": ("tick_analysis", lambda m: m["tick_analysis"]["fills"],
                   ["date", "code", "side", "price", "quantity", "notional", "tick_half_spread_bp", "tick_scenario_cost",
                    "price_band", "accounted_interval", "execution_context"]),
    "holding-lots": ("holding_analysis", lambda m: m["holding_analysis"]["closed"] + m["holding_analysis"]["open"],
                     ["code", "entry_date", "exit_date", "marked_at", "quantity", "entry_price", "exit_or_mark_price",
                      "calendar_days", "snapshot_intervals", "gross_pnl", "fees", "net_pnl", "tick_scenario_cost",
                      "net_after_tick_scenario", "right_censored"]),
}


def table(result, kind):
    """(rows, columns) for one evidence table, or None when this audit did not produce it."""
    if kind not in TABLES:
        return None
    module, rows, columns = TABLES[kind]
    if module not in result.get("modules", {}):
        return None
    found = rows(result["modules"])
    return None if found is None else (found, columns)


def available(result):
    return [k for k in TABLES if table(result, k) is not None]
