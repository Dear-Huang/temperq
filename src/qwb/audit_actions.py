"""Turn measured audit modules into action cards.

Each card states what was found, the candidate changes (always including
"keep as is"), what is saved and what may be given up, how strong the
evidence is, and what would overturn the conclusion. Cards only reuse numbers
already computed by the audit modules; nothing here re-runs the strategy.
"""
from __future__ import annotations

# Evidence levels, from strongest to weakest.
EVIDENCE = {
    "recomputed": "复算：按导出成交逐笔重算",
    "scenario": "情景：在声明假设下估计",
    "descriptive": "描述：事后分组，不证明改了会更好",
    "needs_data": "待复验：缺少重放所需材料",
}
STATES = {"act": "建议处理", "review": "建议核对", "hold": "暂不改"}


def _yuan(value):
    return f"¥{value:,.0f}"


def _tick_card(tick):
    total = tick.get("accounted_intervals") or {}
    if tick.get("status") != "verified" or not total.get("traded_notional"):
        return None
    net, cost = total["net_pnl"], total["tick_scenario_cost"]
    bp, after = total["weighted_half_spread_bp"], total["net_after_tick_scenario"]
    fraction = total.get("break_even_cost_fraction")
    bands = tick.get("price_bands") or []
    cheap = next((b for b in bands if b["id"] == "under_5"), None)
    covers = total.get("profit_covers_scenario", after >= 0)
    options = [{"label": "维持原样", "effect": "先用券商成交明细或报价数据核实实际价差，再决定是否改"}]
    if cheap and cheap["traded_notional"]:
        options.append({"label": "少做低价股",
                        "effect": f"低于 5 元的成交占成交额 {cheap['notional_share']:.0%}，每边约 {cheap['weighted_half_spread_bp']:.1f}bp，"
                                  f"约占价差情景 {_yuan(cheap['tick_scenario_cost'])}；改哪一行见『低价股是否太多』"})
    options.append({"label": "降低换手、延长持有", "effect": "直接减少需要付价差的成交次数；改哪一行见『持有期是否太短』"})
    if fraction is None or fraction <= 0:
        break_even = "现有净盈亏不足以覆盖任何正的价差情景"
    elif fraction < 1:
        break_even = f"现有净盈亏只够覆盖约 {fraction:.0%} 的价差情景，相当于每边约 {bp * fraction:.1f}bp"
    else:
        break_even = f"扣除价差情景后仍盈利；实际价差要到每边约 {bp * fraction:.0f}bp 才会吃掉全部盈利"
    return {
        "id": "tick_floor", "state": "act" if not covers else "hold", "title": "回测收益能否覆盖买卖价差",
        "finding": f"回测净盈亏 {_yuan(net)}；按最小价位 0.01 元估计，每边价差约 {bp:.1f}bp（按成交额加权），"
                   f"合计约 {_yuan(cost)}。若原回测未计价差或滑点，扣除后约 {_yuan(after)}。",
        "headline": break_even,
        "options": options,
        "saving": "价差无法省掉，只能通过少交易、避开低价股来减少",
        "sacrifice": "剔除低价股或少交易，可能放弃这部分股票原本贡献的收益",
        "net": f"扣价差情景后 {_yuan(after)}" + ("（为负）" if after < 0 else ""),
        "evidence": "scenario",
        "basis": "交易所最小报价单位 0.01 元，是规则而非模型拟合；方法见作者 STRUCT 稿（SSRN，未经同行评审）",
        "overturn": "原回测已按价差或滑点扣费；或实际成交明显好于半个价位（例如集合竞价撮合）。有报价或成交明细时可校准。",
    }


def _holding_card(holding, edits):
    bands = holding.get("closed_age_bands") or []
    summary = holding.get("closed_summary") or {}
    if holding.get("status") != "verified" or not summary.get("matches"):
        return None
    rows = [{"label": b["label"], "matches": b["matches"], "net": b["net_pnl"],
             "net_after_tick": b["net_after_tick_scenario"]} for b in bands if b["matches"]]
    short = [b for b in bands if b["id"] in {"0_to_2", "3_to_5"}]
    short_n = sum(b["matches"] for b in short)
    long_n = sum(b["matches"] for b in bands if b["id"] not in {"0_to_2", "3_to_5"})
    short_after = sum(b["net_after_tick_scenario"] for b in short)
    short_cost = sum(b["fees"] + b["tick_scenario_cost"] for b in short)
    if not short_n:
        return None
    losing = short_after < 0
    few = long_n < max(5, short_n // 4)
    return {
        "id": "holding_period", "state": "review" if losing else "hold", "title": "持有期是否太短",
        "finding": f"已平仓 {summary['matches']} 笔中，持有 5 个自然日以内的有 {short_n} 笔，付出费用和价差约 {_yuan(short_cost)}，"
                   f"扣完后合计 {_yuan(short_after)}；持有更久的 {long_n} 笔" + ("，样本少，不能下结论。" if few else "。"),
        "headline": (f"短持有交易扣完成本后亏 {_yuan(-short_after)}，值得试着拿久一点" if losing
                     else f"短持有交易扣完成本后仍赚 {_yuan(short_after)}，拿久一点可能丢掉这部分，暂不改"),
        "edits": edits.get("holding", []),
        "table": rows,
        "options": [{"label": "维持原样", "effect": "持有期由选股信号决定时，先确认信号本身能管几天"},
                    {"label": "延长持有期或加排名保留带", "effect": "排名小幅下滑不卖，减少来回交易"}],
        "saving": f"短持有部分的成本约 {_yuan(short_cost)}；拿得更久，成交次数和这部分成本会同比例下降",
        "sacrifice": f"这些短持有交易扣完成本后合计 {_yuan(short_after)}；信号若只在几天内有效，拿久了会丢掉它",
        "net": "需要重放才能估计：补每日打分后，可在同一信号下比较不同持有期",
        "evidence": "descriptive",
        "basis": "按先进先出匹配买卖，费用按股数分摊；分组是事后描述",
        "overturn": "按每日打分重放后，延长持有期的净结果不优于原规则。",
    }


def _fee_card(fees, edits):
    reported, modeled = fees.get("reported_fee"), fees.get("modeled_fee")
    if fees.get("status") not in {"measured", "verified"} or reported is None or modeled is None:
        return None
    gap = reported - modeled
    if abs(gap) < max(1.0, 0.01 * modeled):
        return None
    more = gap > 0
    return {
        "id": "fee_setting", "state": "review", "title": "回测费用设置",
        "finding": f"回测报告费用 {_yuan(reported)}，按你确认的佣金和成交日适用的印花税复算为 {_yuan(modeled)}，"
                   f"{'多' if more else '少'}扣 {_yuan(abs(gap))}。",
        "headline": ("回测扣费偏高，结果偏保守" if more else "回测扣费偏低，结果偏乐观"),
        "edits": edits.get("fees", []),
        "options": [{"label": "维持原样", "effect": "保守的费用不会让结果虚高"},
                    {"label": "按券商实际费率改费用设置后回原平台重测",
                     "effect": "原因见上方『改代码哪里』：佣金率偏高，或卖出仍按 2023-08-28 之前的 10bp 印花税" if more else "检查是否漏算印花税或最低佣金"}],
        "saving": "不改变实际交易成本，只让回测结果更接近实盘",
        "sacrifice": "无",
        "net": f"回测净盈亏约{'低估' if more else '高估'} {_yuan(abs(gap))}",
        "evidence": "recomputed",
        "basis": "佣金按确认的费率和最低佣金，印花税按成交日期适用税率",
        "overturn": "你的券商合同费率与确认值不同；或报告费用中包含过户费等其他项目。",
    }


def _minimum_card(fees, edits):
    premium, count = fees.get("minimum_premium") or 0, fees.get("small_unit_count") or 0
    if fees.get("status") not in {"measured", "verified"} or premium <= 0 or not count:
        return None
    return {
        "id": "minimum_commission", "state": "review" if premium >= 500 else "hold", "title": "小额订单的最低佣金",
        "finding": f"{count} 笔订单金额低于最低佣金拐点，因最低佣金多付约 {_yuan(premium)}。",
        "headline": "金额小，影响有限" if premium < 500 else "小单最低佣金值得处理",
        "edits": edits.get("order_floor", []),
        "options": [{"label": "维持原样", "effect": "金额不大时可以不改"},
                    {"label": "设置最低新增买入金额", "effect": "可在下方『按审计发现修改代码』生成改法"}],
        "saving": f"最多约 {_yuan(premium)}",
        "sacrifice": "跳过小单会多留现金、增加与目标的偏离",
        "net": "取决于跳过的那部分仓位后来涨跌",
        "evidence": "recomputed",
        "basis": "每笔佣金按比例与最低佣金取大值",
        "overturn": "订单在原平台实际是合并成交，或券商不收最低佣金。",
    }


def _price_card(holding, tick, edits):
    """Low-priced stocks pay the widest tick spread; say whether they still earned their keep."""
    bands = {b["id"]: b for b in holding.get("entry_price_bands") or []}
    cheap = (bands.get("under_5") or {}).get("closed") or {}
    share = next((b["notional_share"] for b in tick.get("price_bands") or [] if b["id"] == "under_5"), 0)
    if holding.get("status") != "verified" or not cheap.get("matches") or share < .05:
        return None
    after, cost = cheap["net_after_tick_scenario"], cheap["fees"] + cheap["tick_scenario_cost"]
    losing = after < 0
    return {
        "id": "low_price", "state": "review" if losing else "hold", "title": "低价股是否太多",
        "finding": f"低于 5 元的股票占成交额 {share:.0%}，已平仓 {cheap['matches']} 笔，付出费用和价差约 {_yuan(cost)}，扣完后合计 {_yuan(after)}。",
        "headline": (f"低价股扣完成本后亏 {_yuan(-after)}，可以加价格门槛" if losing
                     else f"低价股扣完成本后仍赚 {_yuan(after)}，加价格门槛会丢掉这部分，暂不改"),
        "edits": edits.get("price", []),
        "options": [{"label": "维持原样", "effect": "低价股本身赚钱时，价差只是成本的一部分"},
                    {"label": "选股时排除 5 元以下", "effect": "直接去掉价差最贵的一段"}],
        "saving": f"这部分的费用和价差约 {_yuan(cost)}",
        "sacrifice": f"这些交易扣完成本后合计 {_yuan(after)}，剔除后由其他股票顶上，结果要重跑才知道",
        "net": "需回原平台重跑，用效果验证比较",
        "evidence": "descriptive",
        "basis": "按买入价分组，先进先出匹配买卖；价差按最小价位情景",
        "overturn": "剔除后补进来的股票表现更差；或低价股实际成交价差小于半个价位。",
    }


def cards(result, bundle=None):
    """Action cards; with the bundle, each card also points at the code lines to change."""
    from . import audit_levers
    modules = result.get("modules", {})
    fees_module = modules.get("fee_analysis") or {}
    edits = {}
    if bundle:
        levers, platform = audit_levers.locate(bundle), bundle.get("adapter", "")
        more = (fees_module.get("reported_fee") or 0) > (fees_module.get("modeled_fee") or 0)
        edits = {"holding": audit_levers.holding(levers, platform),
                 "price": audit_levers.price_floor(levers, platform),
                 "order_floor": audit_levers.order_floor(levers, platform, fees_module.get("breakpoint_notional") or 0),
                 "fees": audit_levers.fees(levers, bundle, more, (result.get("settings") or {}).get("commission_bp"))}
    out = [_tick_card(modules.get("tick_analysis") or {}),
           _holding_card(modules.get("holding_analysis") or {}, edits),
           _price_card(modules.get("holding_analysis") or {}, modules.get("tick_analysis") or {}, edits),
           _fee_card(fees_module, edits),
           _minimum_card(fees_module, edits)]
    order = {"act": 0, "review": 1, "hold": 2}
    return sorted((c for c in out if c), key=lambda c: order[c["state"]])
