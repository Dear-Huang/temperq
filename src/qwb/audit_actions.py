"""Turn measured audit modules into action cards.

Each card states what was found, the candidate changes (always including
"keep as is"), what is saved and what may be given up, how strong the
evidence is, and what would overturn the conclusion. Cards only reuse numbers
already computed by the audit modules; nothing here re-runs the strategy.
"""
from __future__ import annotations

from .audit_rules import EVIDENCE, cite

# Old evidence names → levels in docs/specs/AUDIT_EVIDENCE_SPEC.md §3.
_LEVEL = {"recomputed": "E1", "descriptive": "E2", "scenario": "E3", "needs_data": "N"}
# Rules each card rests on.
_RULES = {
    "tick_floor": ("F-TICK-STOCK", "F-TICK-FUND", "M-TICK-HALF", "M-SLIPPAGE"),
    "holding_period": ("M-FIFO", "T-HOLD-SHORT"),
    "low_price": ("M-FIFO", "T-PRICE-LOW", "F-TICK-STOCK", "M-TICK-HALF"),
    "fee_setting": ("F-STAMP", "F-MIN-COMMISSION"),
    "minimum_commission": ("F-MIN-COMMISSION", "T-MIN-COMM-MATERIAL"),
    "rank_churn": ("M-RANK-REPLAY", "M-FIFO", "T-BUFFER-RANK", "T-WHIPSAW-DAYS", "T-CHURN-SHARE", "T-CHURN-MIN"),
    "cash_buffer": ("T-CASH-SHORT", "T-CASH-BUFFER"),
    "fill_timing": ("M-FILL-TIMING", "T-TIMING-SHARE"),
    "partial_entry": ("M-PARTIAL-ENTRY", "T-PARTIAL-DAYS"),
    "failed_exits": ("M-ACTIVITY",),
    "idle": ("M-ACTIVITY",),
    "fill_quality": ("M-FILL-RANGE", "M-FILL-FLOOR", "T-FILL-BEST-SHARE"),
    "sequence": ("M-SEQUENCE",),
}
STATES = {"act": "建议处理", "review": "建议核对", "hold": "暂不改"}


def _clock(text):
    from .audit_timing import hhmm
    return hhmm(text)


def _yuan(value):
    return f"¥{value:,.0f}"


def _sequence_card(sequence):
    """Trades whose order of events the backtest took for granted: sale proceeds, signal time, code hints."""
    if not sequence:
        return None
    fund = sequence.get("funding") or {}
    late = sequence.get("signal_after_fill") or {}
    hints = sequence.get("code_hints") or {}
    parts = [x["verdict"] for x in (late, fund, hints) if x and (x.get("late") or x.get("dependent") or x.get("hints"))]
    if not parts:
        return None
    serious = late.get("late", 0) > 0 or fund.get("partial_sell_days", 0) > 0
    return {
        "id": "sequence", "state": "act" if late.get("late") else "review", "title": "先后顺序是否成立",
        "finding": "；".join(parts) + "。",
        "headline": ("有买入用了下单时还不存在的信号" if late.get("late") else
                     "换仓的买入依赖同一时刻卖出的钱，且有卖单只成交了一部分" if serious else
                     "换仓的买入依赖同一时刻卖出的回款：卖单没成交完时，这笔买入付不起"),
        "edits": [],
        "options": [{"label": "维持原样", "effect": "卖出标的流动性好、单笔远小于成交量，且平台确实先撮合卖单时"},
                    {"label": "按已成交卖单的回款再买", "effect": "先卖、确认成交后用实际到账资金买入；卖单没成交时不买或只买够得上的部分"},
                    {"label": "按下一根分钟线撮合重跑", "effect": "平台改为下一根K线开盘价撮合（米筐 matching_type=next_bar），区分看完行情再成交的乐观程度"}],
        "saving": "不省钱；资金衔接和先后顺序更接近实盘",
        "sacrifice": "卖单没成交的那几次会少买或晚买",
        "net": "需要回原平台改订单管理或撮合方式重跑，用 qwb audit compare 比较",
        "evidence": "recomputed" if late.get("late") else "descriptive",
        "basis": "成交时间、上一收盘的可用现金、打分可用时间、代码文字",
        "overturn": "平台撮合先成交卖单且全部成交；打分时间记录的是写入时刻而不是计算所用数据的时刻。",
    }


def _quality_card(quality):
    """Fill prices that look too good, or a result that does not survive the worst nearby price."""
    if not quality or quality.get("status") != "suspicious":
        return None
    parts = [x["verdict"] for x in (quality.get("range"), quality.get("floor")) if x]
    rng = quality.get("range") or {}
    return {
        "id": "fill_quality", "state": "act", "title": "成交价是否拿得到",
        "finding": "；".join(parts) + "。",
        "headline": ("成交价系统性落在当天最有利的位置，疑似偷看了当天走势" if rng.get("status") == "suspicious"
                     else "按成交后几分钟的最差价重算就不再盈利：收益依赖较好的成交价（压力情景）"),
        "edits": [],
        "options": [{"label": "核对信号所用数据的时间", "effect": "确认选股只用了下单之前已经公开的数据，没有用当天收盘价、全天成交量或当天走势"},
                    {"label": "延后执行重跑", "effect": "信号不动，把执行时点延后到 9:35、10:00，并调高滑点，回平台重跑，用 qwb audit compare 比较"},
                    {"label": "改用更保守的撮合", "effect": "按下一根分钟线或成交量加权均价撮合，限制单笔占当分钟成交量"}],
        "saving": "不省钱；结果更接近实盘能拿到的价格",
        "sacrifice": "回测收益会下降",
        "net": "需要回原平台按更保守的执行重跑",
        "evidence": "scenario",
        "basis": "成交日全天最高最低价与成交后分钟价；股数和费用不变",
        "overturn": "策略确实只用前一日数据、在竞价截止前提交，并且单笔远小于当分钟成交量；或延后执行重跑后结论不变。",
    }


def _exit_card(activity):
    """Sell orders that did not complete while the name stayed held: the exit the backtest planned did not happen."""
    exits = (activity or {}).get("exits") or {}
    if exits.get("failed", 0) < 3:
        return None
    return {
        "id": "failed_exits", "state": "act", "title": "卖单没卖掉",
        "finding": exits["verdict"] + "。",
        "headline": f"{exits['failed']} 笔卖单没成交完、收盘仍持有，最长连续 {exits['longest_run']} 个交易日：计划的退出没有发生",
        "edits": [],
        "options": [{"label": "维持原样", "effect": "持仓过夜本来就在策略允许范围内时"},
                    {"label": "提前退出或分笔卖出", "effect": "收盘前几分钟成交量可能为零，改到更早、成交更活跃的时点，或拆成多笔"},
                    {"label": "卖不掉时次日补卖", "effect": "检查前一日的退出是否完成，没完成就在次日开盘处理"}],
        "saving": "不省钱；持仓风险按策略本意控制",
        "sacrifice": "提前卖出可能错过尾盘的价格",
        "net": "需要回原平台改退出逻辑重跑才能知道",
        "evidence": "recomputed",
        "basis": "委托记录里卖单的下单数与成交数，以及当日收盘持仓",
        "overturn": "策略本来就允许持仓过夜，或这些卖单是可选的减仓而不是必须完成的退出。",
    }


def _idle_card(activity):
    """Long stretches with nothing held: a low drawdown that comes from not being invested."""
    idle = (activity or {}).get("idle") or {}
    from .audit_activity import LONG_IDLE
    if idle.get("longest", 0) < LONG_IDLE:
        return None
    return {
        "id": "idle", "state": "review", "title": "策略长期没持仓",
        "finding": idle["verdict"] + "。",
        "headline": f"最长连续 {idle['longest']} 个交易日没有任何持仓：低回撤可能来自没投资，而不是控制了风险",
        "edits": [],
        "options": [{"label": "维持原样", "effect": "长期空仓是策略的择时设计时"},
                    {"label": "核对空仓的触发条件", "effect": "查看策略判断空仓、决定建仓的代码，确认是信号使然还是状态变量出错（如用浮点数精确相等判断）"}],
        "saving": "不省钱",
        "sacrifice": "无",
        "net": "如果空仓是程序错误，修正后收益和回撤都会变，需重跑",
        "evidence": "recomputed",
        "basis": "每日收盘持仓与账户现金",
        "overturn": "策略的目标持仓在这些天本来就是空仓。",
    }


def _partial_card(completion):
    """A buy that filled in part and was never topped up: the target is held, the order is not complete."""
    if not completion or completion.get("status") != "found":
        return None
    w = completion["worst"]
    return {
        "id": "partial_entry", "state": "review", "title": "买单没成交完，之后也没补",
        "finding": completion["verdict"] + "。",
        "headline": f"{completion['long_episodes']} 笔买单成交不足、之后很久没补单：持有了目标，不等于完成了原买单",
        "edits": [],
        "options": [{"label": "维持原样", "effect": "策略本意就是只在信号日下一次单、不追单时"},
                    {"label": "成交不足时次日补单", "effect": "在下单函数里比较目标股数和实际持仓，差额下次补上"},
                    {"label": "分笔下单", "effect": "单笔超过当根成交量上限时，拆成多根K线成交"}],
        "saving": "不省钱；资金按策略本意投入",
        "sacrifice": "补单会增加成交次数和费用",
        "net": "需要回原平台加上补单逻辑重跑才能知道",
        "evidence": "recomputed",
        "basis": "委托记录的下单数与成交数；期间现金占比来自账户",
        "overturn": "策略本来就设计为不追单；或补单后的重跑收益不优于原版。",
    }


def _timing_card(timing):
    """Does the result depend on the minute the orders filled? Only from minute prices the user supplied."""
    if not timing or timing.get("status") not in ("sensitive", "stable"):
        return None
    sensitive = timing["status"] == "sensitive"
    judged = set(timing.get("judged_delays") or [])
    delayed = [r for r in timing["rows"] if r.get("delay") in judged] or [r for r in timing["rows"] if r.get("delay")]
    worst = min(delayed, key=lambda r: r["gain"])
    best = max(delayed, key=lambda r: r["gain"])
    flatter = timing.get("open_flatters")
    finding = (f"{timing['timed_fills']} 笔有成交时间，其中 {timing['open_fills']} 笔在 {timing['open_by']} 以前成交"
               f"（成交额占 {timing['open_share']:.0%}）。固定股数和费用，把每笔成交往后挪：{worst['label']}盈亏变化 {_yuan(worst['gain'])}，"
               f"{best['label']}盈亏变化 {_yuan(best['gain'])}；回测净盈亏 {_yuan(timing['net_pnl'])}。")
    if timing.get("max_volume_share") is not None:
        finding += f"单笔成交最多占当分钟成交量的 {timing['max_volume_share']:.0%}。"
    return {
        "id": "fill_timing", "state": "review" if sensitive else "hold", "title": "成交时点是否决定了结论",
        "finding": finding,
        "headline": (("每一档延后成交都比回测用的时点差：回测按这个时点成交偏乐观" if flatter else
                      "成交稍晚，盈亏变化和净盈亏同一量级：结论依赖成交时点") if sensitive
                     else "成交稍晚，盈亏变化远小于净盈亏：成交时点不改变结论"),
        "edits": [],
        "options": [{"label": "维持原样", "effect": "实盘确实能在回测时点按该价成交时（例如开盘竞价挂单且金额小）"},
                    {"label": "改为更可执行的时点或价格", "effect": "回原平台改调仓时点或改用开盘价/均价撮合重跑，再到『效果验证』比较"},
                    {"label": "分笔或延后执行", "effect": "单笔占当分钟成交量大时，分到多个分钟成交"}],
        "saving": "不省钱；回测结果更接近实盘能拿到的价格",
        "sacrifice": f"按延后成交计，盈亏变化最多 {_yuan(worst['gain'])}" if worst["gain"] < 0 else "延后成交都不更差",
        "net": "只换成交价、不重跑决策；这是价格敏感性，不是延迟回放，需冻结信号回原平台重跑确认",
        "evidence": "scenario",
        "basis": "按用户提供的分钟价重算成交价；股数、费用和交易日期不变",
        "overturn": "实盘成交记录显示能在回测时点按回测价成交；或换时点重跑后选股和盈亏都不变。",
        "table_kind": "timing",
        "table": [{"label": r["label"], "fills": r["fills"], "gain": r["gain"], "coverage": r["coverage"]} for r in timing["rows"]],
    }


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
    already = total.get("already_charged") or 0.
    if already > .5:
        basis = (f"按最小价位估计，价差下限合计约 {_yuan(total['tick_floor_cost'])}；{tick.get('slippage_note', '原回测已计滑点')}，"
                 f"其中约 {_yuan(already)} 已含在成交价里，只补差额 {_yuan(cost)}（每边约 {bp:.1f}bp）。")
    else:
        basis = (f"按最小价位估计，每边价差约 {bp:.1f}bp（按成交额加权），合计约 {_yuan(cost)}；"
                 f"{tick.get('slippage_note', '代码里没有设置滑点')}。")
    if cost < .5 and already > .5:
        return {
            "id": "tick_floor", "state": "hold", "title": "回测收益能否覆盖买卖价差",
            "finding": f"回测净盈亏 {_yuan(net)}。" + basis,
            "headline": "原回测设置的滑点已经不低于最小价位价差，这一项不用再扣",
            "options": [{"label": "维持原样", "effect": "滑点设置已覆盖价差下限"}],
            "saving": "无", "sacrifice": "无", "net": f"维持 {_yuan(net)}",
            "evidence": "recomputed",
            "basis": "交易所最小报价单位是规则；滑点按代码里的设置换算为每边成本",
            "overturn": "实际成交价差明显大于最小价位，例如成交清淡或单笔金额大。"}
    return {
        "id": "tick_floor", "state": "act" if not covers else "hold", "title": "回测收益能否覆盖买卖价差",
        "finding": f"回测净盈亏 {_yuan(net)}。" + basis + f"扣除后约 {_yuan(after)}。",
        "headline": break_even,
        "options": options,
        "saving": "价差无法省掉，只能通过少交易、避开低价股来减少",
        "sacrifice": "剔除低价股或少交易，可能放弃这部分股票原本贡献的收益",
        "net": f"扣价差情景后 {_yuan(after)}" + ("（为负）" if after < 0 else ""),
        "evidence": "scenario",
        "basis": "交易所最小报价单位 0.01 元，是规则而非模型拟合；方法见作者 STRUCT 稿（SSRN，未经同行评审）",
        "overturn": "原回测已按价差或滑点扣费；或实际成交明显好于半个价位（例如集合竞价撮合）。有报价或成交明细时可校准。",
    }


def _split_lots(holding, keep):
    """Closed FIFO lots split by a threshold; falls back to the fixed bands for old reports without lots."""
    lots = holding.get("closed")
    if lots is None:
        return None
    hit = [l for l in lots if keep(l)]
    rest = [l for l in lots if not keep(l)]
    total = lambda rows, key: sum(r[key] for r in rows)
    return {"n": len(hit), "rest": len(rest), "after": total(hit, "net_after_tick_scenario"),
            "cost": total(hit, "fees") + total(hit, "tick_scenario_cost")}


def _holding_card(holding, edits, t=None):
    bands = holding.get("closed_age_bands") or []
    summary = holding.get("closed_summary") or {}
    if holding.get("status") != "verified" or not summary.get("matches"):
        return None
    days = int((t or {}).get("T-HOLD-SHORT", 5))
    rows = [{"label": b["label"], "matches": b["matches"], "net": b["net_pnl"],
             "net_after_tick": b["net_after_tick_scenario"]} for b in bands if b["matches"]]
    split = _split_lots(holding, lambda l: l["calendar_days"] <= days)
    if split is not None:
        short_n, long_n, short_after, short_cost = split["n"], split["rest"], split["after"], split["cost"]
    else:
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
        "finding": f"已平仓 {summary['matches']} 笔中，持有 {days} 个自然日以内的有 {short_n} 笔，付出费用和价差约 {_yuan(short_cost)}，"
                   f"扣完后合计 {_yuan(short_after)}；持有更久的 {long_n} 笔" + ("，样本少，不能下结论。" if few else "。"),
        "headline": (f"短持有交易扣完成本后亏 {_yuan(-short_after)}，值得试着拿久一点" if losing
                     else f"短持有交易扣完成本后仍赚 {_yuan(short_after)}，拿久一点可能丢掉这部分，暂不改"),
        "edits": edits.get("holding", []),
        "table": rows,
        "options": [{"label": "维持原样", "effect": "持有期由选股信号决定时，先确认信号本身能管几天"},
                    {"label": "延长持有期或加排名保留带", "effect": "排名小幅下滑不卖，减少来回交易"}],
        "saving": f"短持有部分的成本约 {_yuan(short_cost)}；拿得更久，成交次数和这部分成本会同比例下降",
        "sacrifice": f"这些短持有交易扣完成本后合计 {_yuan(short_after)}；信号若只在几天内有效，拿久了会丢掉它",
        "net": "需要重放才能估计：用同一信号回原平台按不同持有期重跑，再到『效果验证』比较",
        "evidence": "descriptive",
        "basis": "按先进先出匹配买卖，费用按股数分摊；分组是事后描述",
        "overturn": "按每日打分重放后，延长持有期的净结果不优于原规则。",
    }


def _fee_card(fees, edits, model=None, t=None):
    if model:
        return _fee_model_card(model, edits, t or {})
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
                     "effect": "原因见上方『改代码哪里』：佣金率偏高，或卖出仍按 2023-08-28 之前的 10bp 印花税" if more
                               else "回测佣金低于审计默认值时，先按券商合同确认哪边对，再改回测或审计设置"}],
        "saving": "不改变实际交易成本，只让回测结果更接近实盘",
        "sacrifice": "无",
        "net": f"回测净盈亏约{'低估' if more else '高估'} {_yuan(abs(gap))}",
        "evidence": "recomputed",
        "basis": "佣金按确认的费率和最低佣金，印花税按成交日期适用税率",
        "overturn": "你的券商合同费率与确认值不同；或报告费用中包含过户费等其他项目。",
    }


def _fee_model_card(model, edits, t):
    """Spec R3: (1) did the backtest charge its own declared model; (2) against the broker, only once confirmed."""
    declared, check, stamp = model["declared"], model["self_check"], model.get("stamp_check")
    reported = model["charged"]["reported"]
    scenarios = "、".join(f"{s['commission_bp']:g}bp 约 {_yuan(s['fee'])}" for s in model["scenarios"])
    lines = []
    if declared["status"] == "none":
        lines.append("代码里没有读到费用设置：可能用的是平台默认费率，也可能写在平台设置或运行配置里。"
                     "无法核对回测是否按自己的设置扣费；若有运行配置（如 RQAlpha 的 config.json），请一并上传。")
    else:
        where = "；".join(m["source"] + (f"（第 {m['line']} 行）" if m.get("line") else "（运行配置）")
                         for m in declared["models"].values()) or "仅定位到配置键"
        lines.append(f"回测声明的费用模型：{where}。")
        for cls, why in declared["missing"].items():
            lines.append(f"{ {'stock': '股票', 'fund': 'ETF/基金', 'bond': '债券'}[cls] }：{why}。")
        if check["status"] == "consistent":
            lines.append(f"按声明模型逐笔复算 {check['priced_fills']} 笔，与回测实际扣费 {_yuan(reported)} 一致。")
        elif check["status"] == "inconsistent":
            lines.append(f"按声明模型逐笔复算为 {_yuan(check['recomputed'])}，回测实际扣费 {_yuan(check['reported'])}，"
                         f"{check['mismatched_fills']} 笔对不上，差 {_yuan(check['gap'])}。")
    tax_wrong = stamp and stamp["mismatched_sells"]
    if tax_wrong:
        if stamp.get("source") == "成交导出的税费列":
            lines.append(f"成交导出的税费列：卖出股票实收印花税约 {stamp['declared_bp']:.1f}bp，成交日适用税率为 "
                         f"{'/'.join(f'{b:g}' for b in stamp['rule_bp'])}bp，{stamp['mismatched_sells']}/{stamp['sells']} 笔不符；"
                         f"实收 {_yuan(stamp['charged'])}，按适用税率应为 {_yuan(stamp['due'])}，"
                         + (f"回测多扣 {_yuan(stamp['charged'] - stamp['due'])}。" if stamp['charged'] >= stamp['due']
                            else f"回测少扣 {_yuan(stamp['due'] - stamp['charged'])}。"))
        else:
            lines.append(f"声明的卖出印花税约 {stamp['declared_bp']:.1f}bp，与成交日适用税率 {'/'.join(f'{b:g}' for b in stamp['rule_bp'])}bp "
                         f"不符的卖出 {stamp['mismatched_sells']} 笔。")
    broker, versus = model["broker"], model.get("broker_check")
    gap_min, gap_share = t.get("T-FEE-GAP-MIN", 1.), t.get("T-FEE-GAP-SHARE", .01)
    if versus is not None:
        gap = versus["gap"]
        material = abs(gap) >= max(gap_min, gap_share * versus["modeled"])
        lines.append(f"按你确认的合同口径（{broker['commission_bp']:g}bp、最低 ¥{broker['minimum_commission']:g}）复算为 "
                     f"{_yuan(versus['modeled'])}，回测{'高于' if gap > 0 else '低于'}合同口径 {_yuan(abs(gap))}。")
        headline = (f"回测成本{'高于' if gap > 0 else '低于'}合同口径约 {_yuan(abs(gap))}，结果偏{'保守' if gap > 0 else '乐观'}"
                    if material else "回测成本与合同口径一致")
        evidence, conditions = "E1", []
    else:
        lines.append(f"券商合同费率未确认；按费率情景复算：{scenarios}。确认合同费率后才判断回测成本偏高还是偏低。")
        material = False
        headline = ("回测按自己声明的模型扣费一致；合同费率未确认，只给情景区间" if check["status"] == "consistent"
                    else "合同费率未确认，只给情景区间")
        evidence, conditions = "E3c", ["券商合同费率未确认"]
    inconsistent = check["status"] == "inconsistent"
    if not (inconsistent or tax_wrong or material or declared["status"] != "complete" or versus is None):
        return None
    return {
        "id": "fee_setting", "state": "act" if inconsistent or tax_wrong else "review" if material or versus is None else "hold",
        "title": "回测费用设置",
        "finding": "".join(lines),
        "headline": ("回测没有按自己声明的费用模型扣费" if inconsistent else "卖出印花税与成交日税率不符" if tax_wrong else headline),
        "edits": edits.get("fees", []) if inconsistent or tax_wrong or material else [],
        "options": [{"label": "维持原样", "effect": "费用设置与合同一致时不用改"},
                    {"label": "在审计设置确认券商合同费率", "effect": "确认后给出回测高于或低于合同口径的单一结论"}],
        "saving": "不改变实际交易成本，只让回测结果更接近合同口径",
        "sacrifice": "无",
        "net": (f"回测净盈亏与合同口径相差约 {_yuan(abs(versus['gap']))}" if versus is not None else "取决于合同费率，见情景区间"),
        "evidence": evidence, "conditions": conditions,
        "basis": "声明模型来自代码或配置；实际扣费来自成交导出；印花税按成交日适用税率",
        "overturn": "合同还有过户费、经手费等其他项目；或平台默认费率与此处读到的设置不同。",
    }


def _minimum_card(fees, edits, t=None):
    premium, count = fees.get("minimum_premium") or 0, fees.get("small_unit_count") or 0
    material = (t or {}).get("T-MIN-COMM-MATERIAL", 500.)
    if fees.get("status") not in {"measured", "verified"} or premium <= 0 or not count:
        return None
    return {
        "id": "minimum_commission", "state": "review" if premium >= material else "hold", "title": "小额订单的最低佣金",
        "finding": f"{count} 笔订单金额低于最低佣金拐点，因最低佣金多付约 {_yuan(premium)}。",
        "headline": f"金额小于 {_yuan(material)}，影响有限" if premium < material else "小单最低佣金值得处理",
        "edits": edits.get("order_floor", []) if premium >= material else [],
        "options": [{"label": "维持原样", "effect": "金额不大时可以不改"},
                    {"label": "设置最低新增买入金额", "effect": "可在下方『按审计发现修改代码』生成改法"}],
        "saving": f"最多约 {_yuan(premium)}",
        "sacrifice": "跳过小单会多留现金、增加与目标的偏离",
        "net": "取决于跳过的那部分仓位后来涨跌",
        "evidence": "recomputed",
        "basis": "每笔佣金按比例与最低佣金取大值",
        "overturn": "订单在原平台实际是合并成交，或券商不收最低佣金。",
    }


def _price_card(holding, tick, edits, t=None):
    """Low-priced stocks pay the widest tick spread; say whether they still earned their keep."""
    from .securities import asset_class
    floor = float((t or {}).get("T-PRICE-LOW", 5.))
    cheap_stock = lambda code, price: asset_class(code) == "stock" and price < floor
    fills = tick.get("fills")
    if fills is not None:
        total_value = sum(f["notional"] for f in fills)
        share = sum(f["notional"] for f in fills if cheap_stock(f["code"], f["price"])) / total_value if total_value else 0
    else:
        share = next((b["notional_share"] for b in tick.get("price_bands") or [] if b["id"] == "under_5"), 0)
    split = _split_lots(holding, lambda l: cheap_stock(l["code"], l["entry_price"]))
    if split is not None:
        cheap = {"matches": split["n"], "net_after_tick_scenario": split["after"], "cost": split["cost"]}
    else:
        bands = {b["id"]: b for b in holding.get("entry_price_bands") or []}
        cheap = (bands.get("under_5") or {}).get("closed") or {}
        if cheap:
            cheap = {**cheap, "cost": cheap["fees"] + cheap["tick_scenario_cost"]}
    if holding.get("status") != "verified" or not cheap.get("matches") or (share or 0) < .05:
        return None
    after, cost = cheap["net_after_tick_scenario"], cheap["cost"]
    losing = after < 0
    return {
        "id": "low_price", "state": "review" if losing else "hold", "title": "低价股是否太多",
        "finding": f"低于 {floor:g} 元的股票占成交额 {share:.0%}，已平仓 {cheap['matches']} 笔，付出费用和价差约 {_yuan(cost)}，扣完后合计 {_yuan(after)}。",
        "headline": (f"低价股扣完成本后亏 {_yuan(-after)}，可以加价格门槛" if losing
                     else f"低价股扣完成本后仍赚 {_yuan(after)}，加价格门槛会丢掉这部分，暂不改"),
        "edits": edits.get("price", []),
        "options": [{"label": "维持原样", "effect": "低价股本身赚钱时，价差只是成本的一部分"},
                    {"label": f"选股时排除 {floor:g} 元以下", "effect": "直接去掉价差最贵的一段"}],
        "saving": f"这部分的费用和价差约 {_yuan(cost)}",
        "sacrifice": f"这些交易扣完成本后合计 {_yuan(after)}，剔除后由其他股票顶上，结果要重跑才知道",
        "net": "需回原平台重跑，用效果验证比较",
        "evidence": "descriptive",
        "basis": "按买入价分组，先进先出匹配买卖；价差按最小价位情景",
        "overturn": "剔除后补进来的股票表现更差；或低价股实际成交价差小于半个价位。",
    }


def _cash_buffer_card(signals, edits, t=None):
    if not signals or signals.get("cash_short_orders", 0) < 3:
        return None
    ex = signals["cash_short_examples"][0]
    keep = (t or {}).get("T-CASH-BUFFER", .01)
    return {
        "id": "cash_buffer", "state": "review", "title": "全仓下单没留费用余量",
        "finding": f"有 {signals['cash_short_orders']} 笔买单因“可用资金不足”被拒，差额最多 {_yuan(signals['cash_short_gap'])}："
                   f"例如 {ex['date']} 有 {_yuan(ex['available'])}，下单需要 {_yuan(ex['required'])}。"
                   f"策略用全部现金下单，佣金和滑点一加就超了，结果这 {signals['cash_short_days']} 天信号选中的标的没买到"
                   "（账户可能仍持有其他标的）。",
        "headline": f"按原订单金额，下单留约 {keep:.0%} 余量可覆盖这 {signals['cash_short_orders']} 笔缺口；改代码后价格和股数会变，需重跑",
        "edits": edits.get("cash_buffer", []),
        "options": [{"label": "维持原样", "effect": "没买到目标的天数由当天价格碰巧决定，回测结果含这部分偶然"},
                    {"label": f"下单金额乘 {1 - keep:g}", "effect": "留出佣金和滑点，买单不再被拒；比例是经验值"},
                    {"label": "按预算算最大整手数", "effect": "下单前按 价格+滑点、佣金（含最低佣金）和整手单位，算出现金买得起的最大股数再下单；"
                                                          "不需要经验比例，现金留得最少"}],
        "saving": "不省钱：修的是本该买到却没买到目标的天数",
        "sacrifice": f"每次少买约 {keep:.0%} 的金额，现金略多",
        "net": "没买到目标这些天的涨跌原回测没有吃到，改后结果可能更好也可能更差，需回原平台重跑",
        "evidence": "scenario",
        "basis": "委托详情里平台给出的拒单原因和金额",
        "overturn": "这些天恰好是下跌日，空仓反而躲过了亏损；重跑后对比即可看出。",
    }


def _churn_card(signals, edits, opening=None, t=None):
    if not signals or not signals["switches"]:
        return None
    t = t or {}
    avoided, switches = signals["avoided_switches"], signals["switches"]
    worth = avoided >= max(t.get("T-CHURN-MIN", 3), t.get("T-CHURN-SHARE", .2) * switches)
    opening = opening or {}
    known = opening.get("status") == "declared_empty"
    inputs = [("少换次数", f"{avoided}"), ("配对口径", "FIFO"), ("已平仓笔数", f"{signals.get('closed_lots', 0)}"),
              ("期初状态", opening.get("text", "未确认")),
              ("平均每笔成本", f"¥{signals['cost_per_round_trip']:,.2f}（费用 + 价差情景）"),
              ("每笔成本是否随审计佣金变化", "否：取导出的实际费用")]
    retained = signals.get("retained_days", 0)
    missed = signals["days"] - signals["followed_days"] - retained
    groups = signals.get("missed_groups") or []
    reasons = "；".join(f"{g['days']} 天{'全部' if g['days'] > 1 else ''}是 {g['code']}，"
                       f"{(_clock(g['time']) + ' 下单，') if _clock(g['time']) else ''}平台原因：{g['reason'].replace('委托未成交：', '')}"
                       for g in groups[:3]) or "、".join(f"{k} {v} 天" for k, v in signals["missed_reasons"].items())
    if groups and groups[0]["days"] >= max(3, .5 * missed):
        reasons += ("。要确定原因，需要该标的这些日期的分钟数据（区分成交量为 0 和缺数据），"
                    "或原平台同区间的委托和成交导出")
    return {
        "id": "rank_churn", "state": "review" if worth else "hold", "title": "排名一变就换仓",
        "finding": (f"按策略自己的排名，{signals['days']} 个信号日里第一名换了 {signals.get('top_switches', switches)} 次"
                    + (f"，目标组合（约 {signals['target_size']} 只）换了 {switches} 次" if signals.get("target_size", 1) > 1 else "")
                    + f"，平均每 {signals['average_hold_days']:.1f} 个信号日换一次；")
                   + f"每次换仓的费用和价差约 {_yuan(signals['cost_per_round_trip'])}。"
                   f"有 {signals['whipsaw_lots']} 笔卖出后 5 天内又排回第一，这些交易付出约 {_yuan(signals['whipsaw_cost'])}。"
                   + (f"另有 {missed} 天没有持有选中的目标（{reasons}）。" if missed else "持仓每天都跟上了信号。")
                   + (f"{retained} 天没有下单、继续持有紧随第一之后的标的：现象与保留规则一致，但没有策略每天的最终目标记录，"
                      "不能确认是规则还是漏单，未计入上面的天数。" if retained else ""),
        "headline": (f"固定成交下，排进前 {signals['buffer_rank']} 就不卖可少换 {avoided} 次，费用和价差情景约 "
                     f"{_yuan(signals['avoided_cost'])}；收益影响需重跑" if worth
                     else f"固定成交下保留带只能少换 {avoided} 次，暂不改"),
        "inputs": inputs,
        "formula": f"{avoided} × ¥{signals['cost_per_round_trip']:,.2f} = {_yuan(signals['avoided_cost'])}",
        "conditions": [] if known else ["首日持仓成本未确认，换仓节省仅为条件情景；补填期初现金并声明空仓可消除"],
        "edits": edits.get("buffer", []),
        "options": [{"label": "维持原样", "effect": "信号每天都更新，换仓跟得最紧"},
                    {"label": f"加保留带：持有的标的排进前 {signals['buffer_rank']} 就继续持有", "effect": "排名小幅波动不换仓"}],
        "saving": f"少换 {avoided} 次，费用和价差约 {_yuan(signals['avoided_cost'])}",
        "sacrifice": "持有的标的掉到第二时不换，会错过新第一名这几天的涨幅；是赚是亏要重跑才知道",
        "net": "需回原平台重跑，用效果验证比较",
        "evidence": "scenario",
        "basis": "用策略导出的每日排名回放换仓次数；每次换仓成本取已平仓交易的平均费用和价差",
        "overturn": "重跑后加保留带的净收益低于原规则：说明第一名的切换本身在赚钱。",
    }


def attach_reruns(cards, reruns):
    """Same-period reruns of a card (spec R6). They are E4: in-sample, never 'verified'."""
    labels = {"better": "更好", "mixed": "有得有失", "worse": "更差"}
    for card in cards:
        runs = reruns.get(card["id"]) or []
        if not runs:
            continue
        card["reruns"] = [{"state": r["state"], "verdict": r["verdict"], "version": r.get("version"), "id": r.get("id"),
                           "parent": r.get("parent", ""), "label": labels.get(r["state"], r["state"])} for r in runs]
        card["evidence"] = "E4"
        latest = runs[-1]
        if latest["state"] in {"worse", "mixed"}:
            card["state"] = "hold"
            card["headline"] = f"同区间复跑{labels[latest['state']]}（样本内）：{card['headline']}"
        else:
            card["headline"] = f"同区间复跑更好（样本内，不代表样本外有效）：{card['headline']}"
    return cards


def cards(result, bundle=None):
    """Action cards; with the bundle, each card also points at the code lines to change."""
    from . import audit_levers
    from .audit_rules import defaults
    modules = result.get("modules", {})
    fees_module = modules.get("fee_analysis") or {}
    thresholds = {**defaults(), **((result.get("rules") or {}).get("thresholds") or {})}
    edits = {}
    if bundle:
        levers, platform = audit_levers.locate(bundle), bundle.get("adapter", "")
        more = (fees_module.get("reported_fee") or 0) > (fees_module.get("modeled_fee") or 0)
        edits = {"holding": audit_levers.holding(levers, platform),
                 "price": audit_levers.price_floor(levers, platform),
                 "order_floor": audit_levers.order_floor(levers, platform, fees_module.get("breakpoint_notional") or 0),
                 "fees": audit_levers.fees(levers, bundle, more, (result.get("settings") or {}).get("commission_bp")),
                 "buffer": audit_levers.buffer(levers, (modules.get("signal_mapping") or {}).get("buffer_rank", 2)),
                 "cash_buffer": audit_levers.cash_buffer(levers, thresholds["T-CASH-BUFFER"])}
    out = [_tick_card(modules.get("tick_analysis") or {}),
           _holding_card(modules.get("holding_analysis") or {}, edits, thresholds),
           _price_card(modules.get("holding_analysis") or {}, modules.get("tick_analysis") or {}, edits, thresholds),
           _fee_card(fees_module, edits, modules.get("fee_model"), thresholds),
           _minimum_card(fees_module, edits, thresholds),
           _churn_card(modules.get("signal_mapping"), edits, result.get("opening_state"), thresholds),
           _cash_buffer_card(modules.get("signal_mapping"), edits, thresholds),
           _timing_card(modules.get("fill_timing")),
           _partial_card(modules.get("partial_entry")),
           _exit_card(modules.get("activity")),
           _idle_card(modules.get("activity")),
           _quality_card(modules.get("fill_quality")),
           _sequence_card(modules.get("sequence"))]
    order = {"act": 0, "review": 1, "hold": 2}
    cards = sorted((c for c in out if c), key=lambda c: order[c["state"]])
    for card in cards:
        level = _LEVEL.get(card["evidence"], card["evidence"])
        card["conditions"] = card.get("conditions") or []
        if card["conditions"] and level == "E3":
            level = "E3c"
        card["evidence"] = level
        card["rules"] = cite(*_RULES.get(card["id"], ()))
    return cards
