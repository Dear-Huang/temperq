"""Account-path diagnostics and explicitly conditional, fixed-fill cost scenarios."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time

from .portfolio.reference import Fees
from .audit_fee_units import charging_units
from .securities import pays_stamp_tax


def fee_analysis(bundle, account, settings):
    fees = Fees(settings["commission_bp"], settings["minimum_commission"], 0.)
    units = charging_units(bundle['fills'], fees)
    allocations = {}
    for unit in units:
        for index in unit['indexes']:
            allocations[index] = (unit, unit['modeled_commission'] * bundle['fills'][index]['notional'] / unit['notional'])
    rows, groups, unknown = [], defaultdict(list), 0
    for index, fill in enumerate(bundle["fills"]):
        unit, commission = allocations[index]
        try:
            cost = fees.costs(fill["notional"], fill["side"].upper(), date.fromisoformat(fill["date"]))
            if not pays_stamp_tax(fill["code"]):
                cost["stamp_tax"] = 0.0
        except ValueError:
            cost = None  # Unsupported dates stay unpriced, rather than silently using today's tax.
        if cost is not None:
            cost['commission'] = commission
        expected = sum(cost.values()) if cost is not None else None
        rows.append({**fill, "modeled_commission": cost["commission"] if cost else None,
                     "modeled_stamp_tax": cost["stamp_tax"] if cost else None,
                     "modeled_fee": expected, "reported_minus_modeled": fill["fee"] - expected if expected is not None else None,
                     'charge_basis': unit['basis'], 'charge_confirmed': unit['confirmed'],
                     'commission_allocation': 'notional_proportion_display_only'})
        try:
            clock = time.fromisoformat(fill["time"].strip()).isoformat()
        except (ValueError, TypeError):
            unknown += 1
            continue
        # Equal reported clocks are only grouping candidates, never evidence of one order.
        groups[(fill["date"], clock, fill["code"], fill["side"])].append(index)
    for unit in units:
        priced_rows = [rows[i] for i in unit['indexes']]
        unit['modeled_fee'] = sum(r['modeled_fee'] for r in priced_rows) if all(r['modeled_fee'] is not None for r in priced_rows) else None
        unit['reported_fee'] = sum(r['fee'] for r in priced_rows)
        unit['reported_minus_modeled'] = unit['reported_fee'] - unit['modeled_fee'] if unit['modeled_fee'] is not None else None
        unit['status'] = 'confirmed' if unit['confirmed'] and unit['modeled_fee'] is not None else 'scenario'
    batches = []
    for (day, clock, code, side), indexes in sorted(groups.items()):
        fills = [bundle['fills'][i] for i in indexes]
        if len(fills) < 2:
            continue
        # A partial execution is not a separate order to merge. A clock-sliced
        # subset of an order cannot establish its total commission either.
        order_ids = [f.get('order_id') for f in fills if f.get('order_id')]
        if len(set(order_ids)) != len(order_ids):
            continue
        if any(f.get('order_id') and len(allocations[i][0]['indexes']) > 1 for i, f in zip(indexes, fills)):
            continue
        value = sum(f["notional"] for f in fills)
        separate = sum(max(fees.minimum_commission, f["notional"] * fees.commission_bp / 10000) for f in fills)
        combined = max(fees.minimum_commission, value * fees.commission_bp / 10000)
        saving = max(0., separate - combined)
        if saving <= 1e-8:
            continue
        batches.append({"date": day, "time": clock, "code": code, "side": side,
                        "fill_count": len(fills), "notional": value,
                        "separate_commission": separate, "combined_commission": combined,
                        "conditional_saving": saving, "adverse_price_budget_bp": saving / value * 10000,
                        "refs": [f["ref"] for f in fills]})
    priced = [r for r in rows if r["modeled_fee"] is not None]
    confirmed_units = [u for u in units if u['status'] == 'confirmed']
    mismatches = [u for u in confirmed_units if abs(u['reported_minus_modeled']) > .02]
    gross_value = sum(f["notional"] for f in bundle["fills"])
    included_days = {d["date"] for d in account["daily"]}
    included_value = sum(f["notional"] for f in bundle["fills"] if f["date"] in included_days)
    net = sum(d["net_pnl"] for d in account["daily"])
    return {"id": "fee_analysis", "version": 2, "status": "measured", "fills": rows,
            'charge_units': units, 'confirmed_unit_count': len(confirmed_units),
            'unknown_basis_unit_count': sum(not u['confirmed'] for u in units),
            'unpriced_unit_count': sum(u['modeled_fee'] is None for u in units),
            'confirmed_fee_gap': sum(u['reported_minus_modeled'] for u in confirmed_units),
            'scenario_mismatch_count': sum(abs(r['reported_minus_modeled']) > .02 for r in priced),
            'partial_order_count': sum(u['basis'] == 'order' and u['fill_count'] > 1 for u in units),
            'avoided_duplicate_minimum_scenario': sum(u['per_fill_commission_scenario'] - u['modeled_commission'] for u in units),
            'minimum_premium': sum(u['minimum_premium'] for u in units),
            'small_unit_count': sum(u['minimum_premium'] > 0 for u in units),
            "scope": "all_exported_fills", "reported_fee": sum(f["fee"] for f in bundle["fills"]),
            "modeled_fee": sum(r["modeled_fee"] for r in priced), "priced_fill_count": len(priced),
            "unpriced_fill_count": len(rows) - len(priced), "mismatch_count": len(mismatches),
            "reported_minus_modeled": sum(r["reported_minus_modeled"] for r in priced),
            "absolute_gap": sum(abs(r["reported_minus_modeled"]) for r in priced),
            "commission": sum(r["modeled_commission"] for r in priced),
            "stamp_tax": sum(r["modeled_stamp_tax"] for r in priced),
            "breakpoint_notional": fees.minimum_commission / (fees.commission_bp / 10000) if fees.commission_bp else None,
            "reported_fee_per_traded_bp": sum(f["fee"] for f in bundle["fills"]) / gross_value * 10000 if gross_value else None,
            "batch_candidates": batches, "conditional_batch_saving": sum(r["conditional_saving"] for r in batches),
            "unknown_clock_fills": unknown,
            "batch_conditions": "仅相同日期、相同报告时刻、同证券、同方向的独立收费候选；已识别同单部分成交不列为合单节省。需确认同一授权决策、独立收费且允许合并，保留必需风险退出。保留总量与价格，税不变。不跨日、不买卖轧差。价格预算为节费除以总金额，不模拟等待后的费用或成交变化。",
            "gap_interpretation": "按已声明订单汇总佣金，逐笔佣金按金额分摊仅用于展示；费用差在收费单位汇总核对。只有提供合同/交割单依据、按委托时全部最终成交完整的单位参与异常判定。无依据时按已知委托归并、无委托号时逐笔作情景，不能据此判多收费。差额不是已实现节省，未补造其他费用。",
            "cost_stress": {"scope": "accounted_intervals_fixed_fills_extra_cost", "account_status": account["status"],
                "traded_notional": included_value, "net_pnl": net,
                "positive_profit_exhausted_at_extra_bp": max(0., net) / included_value * 10000 if included_value and account["daily"] else None,
                "scenarios": [{"extra_bp_per_side": bp, "extra_cost": included_value * bp / 10000,
                               "net_after_extra_cost": net - included_value * bp / 10000} for bp in (5., 10.)],
                "meaning": "仅已对账区间，原成交固定，每侧额外成本情景；不重算资金、成交或后续目标，不能当作可执行策略回测。"}}


def risk_analysis(bundle, account, settings):
    positions, contributions = defaultdict(list), defaultdict(list)
    for p in bundle["positions"]:
        positions[p["date"]].append(p)
    for row in account["contributions"]:
        contributions[row["date"]].append(row)
    snapshots = bundle["snapshots"]
    daily = []
    for snapshot in snapshots:
        equity = snapshot["equity"]
        stock_value = sum(p["market_value"] for p in positions[snapshot["date"]])
        shares = [p["market_value"] / stock_value for p in positions[snapshot["date"]]] if stock_value else []
        hhi = sum(w * w for w in shares)
        daily.append({"date": snapshot["date"], "equity": equity,
            "cash_weight": snapshot["cash"] / equity if equity > 0 else None,
            "invested_weight": stock_value / equity if equity > 0 else None,
            "equity_hhi": sum((p["market_value"] / equity) ** 2 for p in positions[snapshot["date"]]) if equity > 0 else None,
            "invested_hhi": hhi if shares else None, "effective_names": 1 / hhi if hhi else None,
            "max_weight": max((p["market_value"] / equity for p in positions[snapshot["date"]]), default=0.) if equity > 0 else None,
            "holdings_count": len(positions[snapshot["date"]])})
    # The peak/trough indices share the accounting baseline. Never insert an invented opening.
    path = ([{"date": None, "equity": account["baseline_equity"]}] + snapshots
            if account["baseline"] == "declared_empty_opening" else snapshots)
    peak, best, peak_i, trough_i = 0, 0., 0, 0
    for i, snapshot in enumerate(path):
        if snapshot["equity"] >= path[peak]["equity"]:
            peak = i
        amount = 1 - snapshot["equity"] / path[peak]["equity"] if path[peak]["equity"] > 0 else 0.
        if amount > best:
            best, peak_i, trough_i = amount, peak, i
    peak_day, trough_day = path[peak_i]["date"], path[trough_i]["date"]
    window_days = {p["date"] for p in path[peak_i + 1:trough_i + 1]}
    stocks = defaultdict(lambda: {"gross_pnl": 0., "fees": 0., "net_pnl": 0.})
    for day in sorted(window_days):
        for row in contributions[day]:
            for field in stocks[row["code"]]:
                stocks[row["code"]][field] += row[field]
    stock_rows = sorted(({"code": code, **values} for code, values in stocks.items()), key=lambda r: r["net_pnl"])
    recovery = next((p["date"] for p in path[trough_i + 1:] if p["equity"] >= path[peak_i]["equity"]), None) if best else None
    cap = settings["max_weight"]
    last = snapshots[-1]
    repair = [{"code": p["code"], "current_weight": p["market_value"] / last["equity"],
               "marked_sale_lower_bound": max(0., p["market_value"] - cap * last["equity"]), "ref": p["ref"]}
              for p in positions[last["date"]] if last["equity"] > 0 and p["market_value"] > cap * last["equity"] + 1e-8]
    known_equity = [r for r in daily if r["cash_weight"] is not None]
    return {"id": "risk_analysis", "version": 1, "status": account["status"], "daily": daily,
            "mean_cash_weight": sum(r["cash_weight"] for r in known_equity) / len(known_equity) if known_equity else None,
            "min_effective_names": min((r["effective_names"] for r in daily if r["effective_names"] is not None), default=None),
            "max_drawdown": {"ratio": best, "peak_date": peak_day, "trough_date": trough_day if best else None,
                "peak_equity": path[peak_i]["equity"], "trough_equity": path[trough_i]["equity"],
                "loss": path[peak_i]["equity"] - path[trough_i]["equity"], "recovery_date": recovery,
                "intervals_to_trough": trough_i - peak_i, "stocks": stock_rows,
                "contribution_gap": sum(r["net_pnl"] for r in stock_rows) - (path[trough_i]["equity"] - path[peak_i]["equity"]),
                "fees": sum(r["fees"] for r in stock_rows)},
            "cap_repair": {"date": last["date"], "max_weight": cap, "positions": repair,
                "marked_sale_lower_bound": sum(r["marked_sale_lower_bound"] for r in repair),
                "meaning": "期末按固定估值、费用前维持权益不变的必要减持金额下界。不是可成交方案：需补当前行情、可卖数量、费用和交易限制后进整数账户规划。"},
            "meaning": "权益路径和收盘集中度描述，非预测风险模型。有效持仓数为股票市值归一化权重平方和的倒数；现金另列。按导出快照间隔计算，不假设连续交易日，不年化。"}


def trading_behavior(bundle):
    grouped = defaultdict(list)
    for f in bundle["fills"]:
        grouped[(f["date"], f["code"])].append(f)
    rows = []
    for (day, code), fills in sorted(grouped.items()):
        buy = sum(f["notional"] for f in fills if f["side"] == "buy")
        sell = sum(f["notional"] for f in fills if f["side"] == "sell")
        if buy and sell:
            rows.append({"date": day, "code": code, "buy_notional": buy, "sell_notional": sell,
                         "two_way_notional": 2 * min(buy, sell), "fees": sum(f["fee"] for f in fills),
                         "refs": [f["ref"] for f in fills]})
    total = sum(f["notional"] for f in bundle["fills"])
    overlap = sum(r["two_way_notional"] for r in rows)
    return {"id": "trading_behavior", "version": 1, "status": "measured", "same_day_two_way": rows,
            "two_way_notional": overlap, "two_way_share": overlap / total if total else None,
            "meaning": "同证券同日双向金额重叠，不是可避免换手或违规认定。A股可卖旧仓再买入；需结合目标版本、订单和可卖库存解释，不能把买卖轧差当免费优化。"}
