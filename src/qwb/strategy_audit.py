"""Deterministic export accounting and replaceable governance analysis modules."""
from __future__ import annotations

from collections import defaultdict

from . import experiments as ex
from .strategy_imports import SCHEMA
from .audit_catalog import coverage
from .audit_diagnostics import fee_analysis, risk_analysis, trading_behavior
from .audit_research import tick_analysis, holding_analysis
from .securities import asset_class

VERSION = "strategy_audit_v1"
TOLERANCE = .05


def _intraday_ok(day_fills, code, start):
    """Could the day's sales of `code` be met in clock order from the opening inventory plus earlier buys?"""
    own = [f for f in day_fills if f["code"] == code]
    if not own or any(not str(f.get("time") or "").strip() for f in own):
        return False
    held = start
    for f in sorted(own, key=lambda f: str(f["time"])):
        held += f["quantity"] if f["side"] == "buy" else -f["quantity"]
        if held < 0:
            return False
    return True


def accounting(bundle):
    positions, fills, actions = defaultdict(dict), defaultdict(list), defaultdict(list)
    for row in bundle["positions"]:
        positions[row["date"]][row["code"]] = row
    for row in bundle["fills"]:
        fills[row["date"]].append(row)
    for row in bundle["actions"]:
        actions[row["date"]].append(row)
    snapshots = bundle["snapshots"]
    empty = bundle["opening"]["kind"] == "empty"
    prior = {} if empty else positions[snapshots[0]["date"]]
    prior_cash = bundle["opening"]["cash"] if empty else snapshots[0]["cash"]
    prior_equity = prior_cash if empty else snapshots[0]["equity"]
    prior_equity = float(prior_equity)
    daily, contributions, checks, same_day = [], [], [], []

    def check(key, gap, day, code="", ref=None, tolerance=TOLERANCE):
        checks.append({"key": key, "gap": float(gap), "date": day, "code": code,
                       "passed": abs(gap) <= tolerance, "ref": ref})

    for index, snapshot in enumerate(snapshots):
        day = snapshot["date"]; current = positions[day]
        check("equity_identity", snapshot["equity"] - snapshot["cash"] - snapshot["market_value"], day, ref=snapshot["ref"])
        check("market_value_total", sum(p["market_value"] for p in current.values()) - snapshot["market_value"], day, ref=snapshot["ref"])
        for code, p in current.items():
            check("position_valuation", p["quantity"] * p["price"] - p["market_value"], day, code, p["ref"])
        flow, fees, action_cash, action_qty, sales = (defaultdict(float) for _ in range(5))
        notional = 0
        for f in fills[day]:
            sign = 1 if f["side"] == "buy" else -1
            rounding = f["quantity"] * .0005 if f.get("notional_source") == "account_cash" else 0
            check("fill_notional", f["quantity"] * f["price"] - f["notional"], day, f["code"], f["ref"],
                  tolerance=TOLERANCE + rounding)
            flow[f["code"]] += sign * f["notional"]
            fees[f["code"]] += f["fee"]
            notional += f["notional"]
            if f["side"] == "sell":
                sales[f["code"]] += f["quantity"]
        for a in actions[day]:
            action_cash[a["code"]] += a["cash"]
            action_qty[a["code"]] += a["quantity_change"]
        # Without an opening declaration, the first close is a baseline, not a fabricated empty account.
        if not empty and index == 0:
            continue
        codes = set(current) | set(prior) | set(flow) | set(action_qty) | set(action_cash)
        quantities = defaultdict(int)
        for f in fills[day]:
            quantities[f["code"]] += f["quantity"] * (1 if f["side"] == "buy" else -1)
        pnl = 0
        for code in sorted(codes):
            p, prev = current.get(code, {}), prior.get(code, {})
            check("inventory", p.get("quantity", 0) - prev.get("quantity", 0) - quantities[code] - action_qty[code], day, code, tolerance=0)
            gross = p.get("market_value", 0) - prev.get("market_value", 0) - flow[code] + action_cash[code]
            net = gross - fees[code]
            contributions.append({"date": day, "code": code, "gross_pnl": gross,
                                  "fees": fees[code], "net_pnl": net})
            pnl += net
            # Necessary inventory check only: no claim to certify the complete T+1 lifecycle.
            start = prev.get("quantity", 0) + max(0, action_qty[code])
            excess = max(0, sales[code] - start)
            if excess and _intraday_ok(fills[day], code, start):
                # Sold out of the same day's earlier buys: legal for T+0 instruments (cross-border, gold, bond and
                # money ETFs, convertible bonds), a T+1 breach for stocks. Not a books error either way.
                same_day.append({"date": day, "code": code, "quantity": excess, "asset": asset_class(code)})
                if asset_class(code) != "stock":
                    excess = 0
            check("prior_inventory_for_sales", excess, day, code, tolerance=0)
        check("cash_flow", snapshot["cash"] - prior_cash + sum(flow.values()) + sum(fees.values()) - sum(action_cash.values()), day)
        check("pnl_bridge", pnl - (snapshot["equity"] - prior_equity), day)
        daily.append({"date": day, "net_pnl": pnl, "equity_change": snapshot["equity"] - prior_equity,
                      "fees": sum(fees.values()), "gross_turnover": notional / prior_equity if prior_equity > 0 else None})
        prior = current; prior_cash = snapshot["cash"]; prior_equity = snapshot["equity"]
    groups = defaultdict(list)
    for row in checks:
        groups[row["key"]].append(row)
    summary = [{"key": key, "count": len(rows), "failures": sum(not r["passed"] for r in rows),
                "max_gap": max(abs(r["gap"]) for r in rows)} for key, rows in groups.items()]
    # Trading-rule exceptions do not invalidate a correctly reconciled P&L decomposition.
    verified = bool(daily) and all(r["passed"] for r in checks if r["key"] != "prior_inventory_for_sales")
    return {"id": "accounting", "version": 1, "status": "verified" if verified else "needs_reconciliation",
            "scope": "export_records_only", "baseline": "declared_empty_opening" if empty else "first_exported_close",
            "baseline_equity": bundle["opening"]["cash"] if empty else snapshots[0]["equity"],
            "summary": summary, "exceptions": [r for r in checks if not r["passed"]],
            "same_day_resales": same_day, "daily": daily, "contributions": contributions}


def attribution(bundle, context):
    account = context["accounting"]
    stocks = defaultdict(lambda: {"gross_pnl": 0., "fees": 0., "net_pnl": 0.})
    for row in account["contributions"]:
        for key in stocks[row["code"]]:
            stocks[row["code"]][key] += row[key]
    base = account["baseline_equity"]
    rows = [{"code": code, **values, "initial_capital_bp": values["net_pnl"] / base * 10000 if base > 0 else None}
            for code, values in stocks.items()]
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    positive = sum(max(0, r["net_pnl"]) for r in rows)
    top = [r for r in rows if r["net_pnl"] > 0]
    net = sum(r["net_pnl"] for r in rows)
    return {"id": "attribution", "version": 1, "status": account["status"], "stocks": rows,
            "net_pnl": net, "gross_pnl": sum(r["gross_pnl"] for r in rows), "fees": sum(r["fees"] for r in rows),
            "positive_total": positive, "negative_total": sum(min(0, r["net_pnl"]) for r in rows),
            "top1_positive_share": top[0]["net_pnl"] / positive if positive else None,
            "top3_positive_share": sum(r["net_pnl"] for r in top[:3]) / positive if positive else None,
            "net_minus_top1_recorded": net - rows[0]["net_pnl"] if rows else None,
            "definition": "end_MV - start_MV - net_buy_notional + logged_action_cash - reported_fees",
            "interpretation": "逐股会计贡献，包含已平仓和未实现盈亏；不能据此区分能力与运气。减去贡献不是剔除该股票后的策略回测。",
            "external_report_status": "unverified_external" if bundle["external_attribution"] else "not_supplied"}


def governance(bundle, context, settings):
    account = context["accounting"]
    commission = settings["commission_bp"] / 10000
    minimum = settings["minimum_commission"]
    costs = context['fee_analysis']
    premium = costs['minimum_premium']
    small_count = costs['small_unit_count']
    snapshots = {s["date"]: s for s in bundle["snapshots"]}
    cap = settings["max_weight"]
    breaches = []
    for p in bundle["positions"]:
        if p["date"] not in snapshots:
            continue  # reported as a coverage gap in the input review
        equity = snapshots[p["date"]]["equity"]
        if equity > 0 and p["market_value"] / equity > cap + 1e-8:
            breaches.append({"date": p["date"], "code": p["code"], "weight": p["market_value"] / equity, "ref": p["ref"]})
    turns = [d["gross_turnover"] for d in account["daily"] if d["gross_turnover"] is not None]
    items = [
        {"key": "concentration", "status": "review" if breaches else "consistent",
         "title": "收盘单票仓位", "observed": len(breaches), "unit": "条超限记录",
         "action": f"按你确认的 {cap:.1%} 上限核对；收盘核对不能证明盘中始终合规", "evidence": breaches},
        {"key": "minimum_commission", "status": "opportunity" if premium > 0 else "consistent",
         "title": "小单费用摩擦", "observed": premium, "unit": "元最低佣金溢价情景（按收费单位）",
         "action": "候选动作：合并同向小额调整、设置免交易带；收益影响需用完整信号和行情重放", "evidence": []},
        {"key": "turnover", "status": "measured" if turns else "needs_input", "title": "交易节奏",
         "observed": sum(turns) / len(turns) if turns else None, "unit": "日均买卖合计 / 前期权益",
         "action": "下一步比较满调、分次调整与免交易带；保持预测不变，分别核对成本与目标偏离", "evidence": []},
        {"key": "inventory", "status": ("review" if any(r["key"] == "prior_inventory_for_sales" for r in account["exceptions"]) else "consistent") if account["daily"] else "needs_input",
         "title": "卖出库存必要条件", "observed": sum(r["key"] == "prior_inventory_for_sales" for r in account["exceptions"]) if account["daily"] else None,
         "unit": "条异常", "action": "按前期库存及已记录公司行动核对；完整 T+1、挂单与盘中状态还需订单生命周期", "evidence": []},
        {"key": "same_day_resale", "status": "review" if account.get("same_day_resales") else "consistent",
         "title": "当日买入当日卖出", "observed": len(account.get("same_day_resales") or []), "unit": "次",
         "action": "确认这些标的可以 T+0：跨境、黄金、债券、货币类 ETF 和可转债可以当日回转，股票和股票型 ETF 不行",
         "evidence": []},
    ]
    return {"id": "governance", "version": 1, "status": "measured", "items": items,
            "settings": settings, "small_fill_count": small_count,
            "scenarios": [{"id": "minimum_fee_friction", "scope": "same_fills_fee_scenario",
                "title": "最低佣金摩擦量化", "value": premium, "unit": "CNY",
                "formula": "sum_by_charge_unit(max(minimum_commission - unit_notional * commission_rate, 0))",
                "meaning": "保持原成交不变，相对无最低佣金的费用差；不是可实现节省，也不是优化后收益"}],
            "code_candidates": bundle["code"].get("candidates", []),
            "log_errors": sum(r["级别"].upper() == "ERROR" for r in bundle["logs"])}


def opening_state(bundle, context):
    """What the audit knows about the account before the first fill (spec R5)."""
    opening = bundle.get("opening") or {}
    lots = (context.get("holding_analysis") or {}).get("closed") or []
    if opening.get("kind") == "empty":
        return {"status": "declared_empty", "cash": opening.get("cash"), "closed_lots": len(lots),
                "text": f"空仓，现金 ¥{opening.get('cash'):,.0f}"}
    first = bundle["snapshots"][0] if bundle.get("snapshots") else None
    held = [p for p in bundle.get("positions", []) if first and p["date"] == first["date"]]
    return {"status": "unconfirmed", "cash": None, "closed_lots": len(lots), "first_day_positions": len(held),
            "text": "未确认：首日收盘持仓按成本未知处理，不进入配对" if held else "未确认：首日无持仓"}


def analyze(bundle, settings=None, *, policy=None):
    if bundle.get("schema_version") != SCHEMA:
        raise ValueError("不支持的策略导入格式")
    settings = dict(settings or {"commission_bp": 3., "minimum_commission": 5., "max_weight": 1.})
    import math
    from . import audit_rules
    broker_confirmed = bool(settings.pop("broker_confirmed", False))
    premise = settings.pop("premise", None) or {}
    claim = settings.pop("claim", None) or {}
    if claim:
        # What a post or vendor says the strategy returned; kept as stated, compared in the report.
        if not isinstance(claim.get("total"), (int, float)) and not isinstance(claim.get("annual"), (int, float)):
            raise ValueError("声称收益须给出累计收益或年化收益")
        claim = {k: claim[k] for k in ("total", "annual", "source") if claim.get(k) not in (None, "")}
    thresholds = audit_rules.resolve(settings.pop("thresholds", None))
    if set(settings) != {"commission_bp", "minimum_commission", "max_weight"}:
        raise ValueError("审计设置字段不完整")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in settings.values()):
        raise ValueError("审计设置须为有限数字")
    if not (0 <= settings["commission_bp"] <= 1000 and 0 <= settings["minimum_commission"] <= 10000 and 0 < settings["max_weight"] <= 1):
        raise ValueError("审计设置超出范围")
    from . import audit_input
    from . import audit_period
    input_review = audit_input.review(bundle)
    period_review = audit_period.assess(bundle, policy)
    if not input_review['can_analyze']:
        raise ValueError('请补充策略代码后再开始审计；代码只读不运行')
    if input_review['mode'] != 'full':
        context = {'partial_diagnostics': audit_input.partial_result(bundle, settings)}
        if bundle['fills'] and not input_review['fills_inferred']:
            context['fee_analysis'] = fee_analysis(bundle, {'daily': [], 'status': 'missing_account'}, settings)
        result = {'schema_version': VERSION, 'input_digest': ex.object_digest(bundle),
                  'settings': settings, 'modules': context, 'input_review': input_review,
                  'period_review': period_review,
                  'mode': input_review['mode'], 'coverage': [], 'coverage_version': 'audit_coverage_v3',
                  'scope': 'static_code_only' if input_review['mode'] == 'static' else 'partial_materials_explicit_scenarios'}
        result['result_digest'] = ex.object_digest(result)
        return result
    context = {}
    context["accounting"] = accounting(bundle)
    context["attribution"] = attribution(bundle, context)
    context["fee_analysis"] = fee_analysis(bundle, context["accounting"], settings)
    context["governance"] = governance(bundle, context, settings)
    context["risk_analysis"] = risk_analysis(bundle, context["accounting"], settings)
    context["trading_behavior"] = trading_behavior(bundle)
    context["tick_analysis"] = tick_analysis(bundle, context["accounting"])
    context["holding_analysis"] = holding_analysis(bundle, context["accounting"], context["tick_analysis"])
    from . import audit_signals, audit_fee_model
    context["fee_model"] = audit_fee_model.analyze(bundle, context, {**settings, "broker_confirmed": broker_confirmed})
    signals = audit_signals.analyze(bundle, context, thresholds)
    from . import audit_premise
    checked = audit_premise.analyze(bundle, premise, period_review.get("partition"), thresholds)
    if checked:
        context["premise"] = checked
    if signals:
        context["signal_mapping"] = signals
    from . import audit_timing
    timing = audit_timing.analyze(bundle, context["accounting"], thresholds)
    if timing:
        context["fill_timing"] = timing
    from . import audit_fill_quality
    quality = audit_fill_quality.analyze(bundle, context["accounting"], thresholds)
    if quality:
        context["fill_quality"] = quality
    from . import audit_sequence
    sequence = audit_sequence.analyze(bundle, context["accounting"], thresholds)
    if sequence:
        context["sequence"] = sequence
    from . import audit_completion
    completion = audit_completion.analyze(bundle, context["accounting"], thresholds)
    if completion:
        context["partial_entry"] = completion
    settings = {**settings, "broker_confirmed": broker_confirmed, **({"premise": premise} if premise else {}),
                **({"claim": claim} if claim else {}),
                **({"thresholds": {k: v for k, v in thresholds.items() if v != audit_rules.defaults()[k]}}
                   if thresholds != audit_rules.defaults() else {})}
    result = {"schema_version": VERSION, "input_digest": ex.object_digest(bundle),
              "settings": settings, "modules": context, "coverage_version": "audit_coverage_v3",
              "rules": audit_rules.snapshot(thresholds),
              "opening_state": opening_state(bundle, context),
              'input_review': input_review, 'mode': 'full',
              'period_review': period_review,
              "coverage": coverage(bundle, {"modules": context}),
              "scope": "export_accounting_and_governance_diagnostics_not_strategy_skill_or_optimized_return"}
    result["result_digest"] = ex.object_digest(result)
    return result
