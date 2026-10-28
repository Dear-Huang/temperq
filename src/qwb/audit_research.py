"""Research-derived diagnostics: tick scenarios and censored FIFO lot attribution.

These do not reconstruct missing predictions or simulate an altered trading policy.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, time
import re

from .struct_costs import tick_half_spread_bp
from .securities import tick_yuan


def _band(price):
    return "under_5" if price < 5 else "5_to_10" if price < 10 else "10_to_20" if price < 20 else "20_plus"


PRICE_BANDS = (("under_5", "低于5元"), ("5_to_10", "5至不足10元"),
               ("10_to_20", "10至不足20元"), ("20_plus", "20元及以上"))


def tick_analysis(bundle, account):
    rows, groups = [], defaultdict(list)
    included = {r["date"] for r in account["daily"]}
    for fill in bundle["fills"]:
        bp = tick_half_spread_bp(fill["price"]) * tick_yuan(fill["code"]) / .01
        cost = fill["notional"] * bp / 10000
        clock = fill["time"].strip()
        phase = ("auction_boundary" if re.fullmatch(r"(?:09:30|15:00)(?::00(?:\.0+)?)?", clock)
                 else "execution_mechanism_unconfirmed")
        row = {**fill, "tick_half_spread_bp": bp, "tick_scenario_cost": cost,
               "price_band": _band(fill["price"]), "accounted_interval": fill["date"] in included,
               "execution_context": phase}
        rows.append(row); groups[row["price_band"]].append(row)
    def aggregate(items):
        value = sum(f["notional"] for f in items)
        cost = sum(f["tick_scenario_cost"] for f in items)
        return {"fills": len(items), "traded_notional": value, "reported_fees": sum(f["fee"] for f in items),
                "tick_scenario_cost": cost, "weighted_half_spread_bp": cost / value * 10000 if value else None}
    all_fills = aggregate(rows)
    active = [r for r in rows if r["accounted_interval"]]
    interval = aggregate(active)
    net = sum(r["net_pnl"] for r in account["daily"])
    interval.update(net_pnl=net, net_after_tick_scenario=net - interval["tick_scenario_cost"],
                    profit_covers_scenario=(net >= interval["tick_scenario_cost"]
                        if account["daily"] and account["status"] == "verified" else None),
                    break_even_cost_fraction=max(0., net) / interval["tick_scenario_cost"]
                        if interval["tick_scenario_cost"] and account["daily"] else None)
    bands = [{"id": key, "label": label, **aggregate(groups[key]),
              "notional_share": sum(f["notional"] for f in groups[key]) / all_fills["traded_notional"] if all_fills["traded_notional"] else None}
             for key, label in PRICE_BANDS]
    observations = {(r["date"], r["code"]): r for r in bundle.get("quote_observations", [])}
    matched = []
    for fill in rows:
        quote = observations.get((fill["date"], fill["code"]))
        if quote:
            matched.append({"date": fill["date"], "code": fill["code"], "fill_ref": fill["ref"],
                "quote_ref": quote["ref"], "notional": fill["notional"], "sample_count": quote["sample_count"],
                "tick_half_spread_bp": fill["tick_half_spread_bp"],
                "quoted_half_spread_bp": quote["quoted_spread_bp"] / 2,
                "below_tick_proxy": quote["quoted_spread_bp"] / 2 < fill["tick_half_spread_bp"] * .98})
    matched_value = sum(r["notional"] for r in matched)
    measured = sum(r["notional"] * r["quoted_half_spread_bp"] for r in matched) / matched_value if matched_value else None
    proxy = sum(r["notional"] * r["tick_half_spread_bp"] for r in matched) / matched_value if matched_value else None
    calibration = {"status": "daily_quote_comparison" if matched else "no_matching_observations" if observations else "not_supplied",
        "matched_fills": len(matched), "matched_notional": matched_value,
        "notional_coverage": matched_value / all_fills["traded_notional"] if all_fills["traded_notional"] else None,
        "weighted_quoted_half_spread_bp": measured, "weighted_tick_half_spread_bp": proxy,
        "below_tick_proxy_fills": sum(r["below_tick_proxy"] for r in matched), "matches": matched,
        "scope": "same_security_same_date_daily_quote_mean_not_trade_time_execution",
        "meaning": "按同证券同日期匹配日均报价价差，按原成交额加权；不是成交时点或集合竞价成本核验。价差均值的样本清洗和时段由来源提供；低于地板代理的记录保留并提示复核，不按地板截断美化。"}
    return {"id": "tick_analysis", "version": 1, "status": account["status"],
            "cost_model": "struct_tick_component_scenario_v1", "tick_yuan": .01,
            "formula": "half_spread_bp = 10000 * 0.01 / (2 * fill_price); cost = notional * half_spread_bp / 10000",
            "fills": rows, "all_exported_fills": all_fills, "accounted_intervals": interval, "price_bands": bands,
            "auction_boundary_fills": sum(r["execution_context"] == "auction_boundary" for r in rows),
            "calibration_status": calibration["status"], "calibration": calibration, "measured_execution_cost": None,
            "meaning": "STRUCT 的一档报价地板分量，按每边半档作固定成交压力情景，非完整流动性模型。成交价代替中间价，仅近似换算；它不是实盘成本下限。开收盘竞价、被动成交和连续竞价机制不同，不能据此断言实盘必亏。",
            "overlap": "费用已保留在净盈亏中；仅在原回测未包含价差/滑点时才能把本情景视为额外成本。若已包含，需先分离，避免重复扣除。",
            "candidate_action": "优先复验减少微调、延长复核间隔，以及低价股准入阈值；本表只量化原成交的摩擦暴露，不把删掉低价股的贡献当优化收益。"}


def holding_analysis(bundle, account, tick):
    """Allocate observed buys to later sales by FIFO; unknown/action lots are censored.

    Day-level sales use only prior-day inventory, preserving the daily A-share
    settlement convention even when source rows are not time ordered.
    """
    dates = [s["date"] for s in bundle["snapshots"]]
    day_index = {day: i for i, day in enumerate(dates)}
    empty = account["baseline"] == "declared_empty_opening"
    excluded = {a["code"] for a in bundle["actions"] if a["quantity_change"] or a["cash"]}
    invalid = {e["code"] for e in account["exceptions"] if e["code"] and e["key"] in {
        "inventory", "prior_inventory_for_sales", "position_valuation", "fill_notional"}}
    excluded |= invalid
    by_day, lots = defaultdict(list), defaultdict(deque)
    excluded_fills, skipped_baseline = [], []
    for fill in tick["fills"]:
        if fill["code"] in excluded:
            excluded_fills.append(fill)
        elif fill["date"] == dates[0] and not empty:
            skipped_baseline.append(fill)
        else:
            by_day[fill["date"]].append(fill)
    if not empty:
        for p in bundle["positions"]:
            if p["date"] == dates[0] and p["code"] not in excluded:
                lots[p["code"]].append({"remaining": p["quantity"], "known": False})
    closed, open_rows, unknown_sales = [], [], []
    unmatched = []
    def record(lot, qty, sell=None, mark=None):
        buy = lot["fill"]
        end = sell["date"] if sell else dates[-1]
        exit_price = sell["price"] if sell else mark["price"]
        buy_value = buy["notional"] * qty / buy["quantity"]
        exit_value = sell["notional"] * qty / sell["quantity"] if sell else qty * exit_price
        entry_fee = buy["fee"] * qty / buy["quantity"]
        exit_fee = sell["fee"] * qty / sell["quantity"] if sell else 0.
        entry_tick = buy["tick_scenario_cost"] * qty / buy["quantity"]
        exit_tick = sell["tick_scenario_cost"] * qty / sell["quantity"] if sell else 0.
        gross = exit_value - buy_value
        net = gross - entry_fee - exit_fee
        return {"code": buy["code"], "entry_date": buy["date"], "exit_date": end if sell else None,
                "marked_at": None if sell else end, "quantity": qty, "entry_price": buy["price"],
                "exit_or_mark_price": exit_price, "entry_notional": buy_value, "exit_or_mark_value": exit_value,
                "calendar_days": (date.fromisoformat(end) - date.fromisoformat(buy["date"])).days,
                "snapshot_intervals": day_index[end] - day_index[buy["date"]],
                "gross_pnl": gross, "entry_fee": entry_fee, "exit_fee": exit_fee,
                "fees": entry_fee + exit_fee, "net_pnl": net,
                "tick_scenario_cost": entry_tick + exit_tick, "net_after_tick_scenario": net - entry_tick - exit_tick,
                "entry_price_band": _band(buy["price"]), "right_censored": sell is None,
                "entry_ref": buy["ref"], "exit_ref": sell["ref"] if sell else mark["ref"]}
    ambiguous_buy_days = 0
    def buy_order(fill):
        try:
            stamp = time.fromisoformat(fill["time"].strip())
            return stamp.hour, stamp.minute, stamp.second, stamp.microsecond
        except (ValueError, TypeError):
            return None
    for day in dates:
        # Only older lots can fund a sale. Same-day buys cannot hide a T+1 data gap.
        for fill in (f for f in by_day[day] if f["side"] == "sell"):
            remaining = fill["quantity"]
            queue = lots[fill["code"]]
            while remaining and queue:
                lot = queue[0]
                qty = min(remaining, lot["remaining"])
                if lot["known"]:
                    closed.append(record(lot, qty, sell=fill))
                else:
                    unknown_sales.append({"code": fill["code"], "date": day, "quantity": qty, "ref": fill["ref"]})
                lot["remaining"] -= qty; remaining -= qty
                if not lot["remaining"]:
                    queue.popleft()
            if remaining:
                unmatched.append({"code": fill["code"], "date": day, "quantity": remaining, "ref": fill["ref"]})
        # Only use clocks if every buy clock is usable; otherwise preserve source order.
        buys = [f for f in by_day[day] if f["side"] == "buy"]
        if all(buy_order(f) is not None for f in buys):
            buys.sort(key=buy_order)
        elif len(buys) > 1:
            ambiguous_buy_days += 1
        for fill in buys:
            lots[fill["code"]].append({"remaining": fill["quantity"], "known": True, "fill": fill})
    last_positions = {p["code"]: p for p in bundle["positions"] if p["date"] == dates[-1]}
    unknown_open = []
    for code in sorted(lots):
        for lot in lots[code]:
            if not lot["known"]:
                unknown_open.append({"code": code, "quantity": lot["remaining"]})
            elif code in last_positions:
                open_rows.append(record(lot, lot["remaining"], mark=last_positions[code]))
            else:
                unmatched.append({"code": code, "date": dates[-1], "quantity": lot["remaining"], "ref": lot["fill"]["ref"]})
    def total(rows):
        return {"matches": len(rows), "quantity": sum(r["quantity"] for r in rows),
                **{key: sum(r[key] for r in rows) for key in ("entry_notional", "gross_pnl", "fees", "net_pnl", "tick_scenario_cost", "net_after_tick_scenario")}}
    def age_band(days):
        return "0_to_2" if days <= 2 else "3_to_5" if days <= 5 else "6_to_10" if days <= 10 else "over_10"
    bands = [{"id": key, "label": label, **total([r for r in closed if age_band(r["calendar_days"]) == key])}
             for key, label in (("0_to_2", "0–2自然日"), ("3_to_5", "3–5自然日"),
                                ("6_to_10", "6–10自然日"), ("over_10", "超过10自然日"))]
    price_bands = [{"id": key, "label": label,
                    "closed": total([r for r in closed if r["entry_price_band"] == key]),
                    "open": total([r for r in open_rows if r["entry_price_band"] == key])}
                   for key, label in PRICE_BANDS]
    qty = sum(r["quantity"] for r in closed)
    eligible = {r["code"] for r in closed + open_rows}
    contribution = sum(r["net_pnl"] for r in account["contributions"] if r["code"] in eligible)
    reconstructed = sum(r["net_pnl"] for r in closed + open_rows)
    comparable = empty and not unmatched
    return {"id": "holding_analysis", "version": 1, "status": account["status"],
            "method": "FIFO_prior_day_inventory_fee_pro_rata", "closed": closed, "open": open_rows,
            "closed_summary": total(closed), "open_summary": total(open_rows),
            "closed_age_bands": bands, "entry_price_bands": price_bands,
            "quantity_weighted_calendar_days": sum(r["calendar_days"] * r["quantity"] for r in closed) / qty if qty else None,
            "quantity_weighted_snapshot_intervals": sum(r["snapshot_intervals"] * r["quantity"] for r in closed) / qty if qty else None,
            "coverage": {"excluded_action_or_invalid_codes": len(excluded), "excluded_fill_count": len(excluded_fills),
                "excluded_notional": sum(f["notional"] for f in excluded_fills),
                "baseline_excluded_fill_count": len(skipped_baseline), "unknown_opening_sale_quantity": sum(r["quantity"] for r in unknown_sales),
                "ambiguous_buy_clock_days": ambiguous_buy_days,
                "unknown_opening_sales": unknown_sales, "unknown_opening_remaining": unknown_open, "unmatched": unmatched,
                "net_bridge_comparable": comparable, "covered_code_net_contribution": contribution if comparable else None,
                "closed_plus_open_net": reconstructed, "net_bridge_gap": reconstructed - contribution if comparable else None},
            "meaning": "按 FIFO 会计分配，买卖费用按股数摊分；不是用户指定批次，也不是信号年龄。自然日与导出快照间隔分列，不能把快照间隔冒充交易日。未平仓单列右删失，不计已平仓胜率或持有期均值。",
            "coverage_note": "有非零公司行动或数量/估值异常的证券整段暂不做批次归因，保留在原逐股归因；未知期初批次不虚构成本或年龄。同日新买不能用于匹配卖出。",
            "policy_note": "hold_days 等代码字面量不作为强制退出依据。按实际持有期分组是事后描述，不能证明延长到5/10天会改善；需要冻结信号、全候选行情和可交易状态再复验。"}
