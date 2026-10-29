"""Coverage map for the strategy audit report.

The catalog is deliberately independent from any model or optimizer. A stage can
be reported as measured, partially measured, or awaiting inputs without turning a
missing input into a negative finding.
"""
from __future__ import annotations

STAGES = (
    {
        "id": "signal_contract", "section": "构建", "title": "预测交付与目标契约",
        "question": "预测端交付的是什么，何时可用，怎样转成目标？",
        "needs": ["带可用时点的分数、排名或目标权重", "股票池和更新节奏", "信号版本"],
        "basis": "信息对象与实现对象必须分开；分数不能直接当成本或收益单位。",
    },
    {
        "id": "universe", "section": "构建", "title": "股票池、准入与规则暴露",
        "question": "哪些证券可以进入组合，过滤规则是否在每个时点可见？",
        "needs": ["历史股票池", "上市、停牌、ST、涨跌停和公司行动状态", "规则生效日期；做因子规则敏感性时另需因子定义与原始行情"],
        "basis": "规则写入信号会改变测量对象，不能只看回测后持仓。",
    },
    {
        "id": "portfolio_mapping", "section": "构建", "title": "目标到组合映射",
        "question": "排名或预测如何变成持仓数量、权重和现金？",
        "needs": ["目标权重或构建公式", "持仓数、单票上限、行业/风格约束", "整手和现金规则"],
        "basis": "构建政策是预测之后的独立决策对象。",
    },
    {
        "id": "constraint_tradeoffs", "section": "构建", "title": "约束冲突与边际代价",
        "question": "仓位、持仓数、现金、整手和交易限制能否同时满足？",
        "needs": ["硬约束与偏好", "当前可卖股数和现金", "完整目标与参考行情"],
        "basis": "不可行时说明冲突；只在明确目标和约束内比较候选，不自动放宽硬规则。",
    },
    {
        "id": "risk_exposure", "section": "构建", "title": "风险与暴露",
        "question": "实际组合承担了哪些集中度、行业、风格和尾部风险？",
        "needs": ["时点持仓", "基准和风格因子", "风险模型或可解释暴露定义"],
        "basis": "不能把组合收益归因给预测，直到暴露与约束被单独量化。",
    },
    {
        "id": "rebalance", "section": "治理", "title": "调仓节奏与调整速度",
        "question": "何时调、调多快、哪些变化暂不交易？",
        "needs": ["历史目标和调仓时间", "免交易带/部分调整规则", "同口径成本和风险"],
        "basis": "调整速度和规则形状需要与预测效果分开比较。",
    },
    {
        "id": "signal_age", "section": "治理", "title": "信号年龄与复核",
        "question": "信号在持仓期间是否仍在允许使用的年龄内？",
        "needs": ["版本化信号历史", "信号可用时间", "未来收益和成本口径"],
        "basis": "不能用一个 IC 零点替代信号有效期的验证。",
    },
    {
        "id": "costs", "section": "实施", "title": "费用、价差与订单规模",
        "question": "费用来自佣金、税、价差还是冲击，小单是否值得调整？",
        "needs": ["实际费率和费用拆分", "决策价、成交价与价差校准", "最低佣金与订单规则"],
        "basis": "观测费用、模型成本和费用情景分账；STRUCT 输出不冒充实测价差。",
    },
    {
        "id": "capacity", "section": "实施", "title": "容量与流动性预算",
        "question": "资金规模和参与预算下，目标能实现多少？",
        "needs": ["成交额/深度和参与率", "订单金额与现金窗口", "受阻和缺口记录"],
        "basis": "容量上限与完成期限是不同问题，日线不能冒充盘口保证。",
    },
    {
        "id": "completion", "section": "实施", "title": "建仓、退出与完成期限",
        "question": "缺多少目标金额，什么时候可达，什么情景下可能延迟？",
        "needs": ["订单目标和受阻记录", "逐日可用现金/流动性预算", "情景供给与退出限制"],
        "basis": "最早可达与保证完成是不同界限；模型日期包络不是未来成交承诺。",
    },
    {
        "id": "execution", "section": "执行", "title": "订单、交易制度与执行偏离",
        "question": "计划怎样变成成交，实际成本和失败原因是什么？",
        "needs": ["订单生命周期和成交回报", "费率、最低佣金、滑点与价位地板", "T+1、涨跌停、批量规则"],
        "basis": "实现短缺和回测成交价是独立的执行对象。",
    },
    {
        "id": "account_integrity", "section": "执行", "title": "账户、现金与公司行动",
        "question": "持仓、现金、成交和公司行动是否闭合？",
        "needs": ["每日权益和现金", "可卖数量、挂单/冻结资金", "分红、送转、配股等事件"],
        "basis": "账户账本先于收益结论；公司行动缺失时归因降级。",
    },
    {
        "id": "attribution", "section": "结果", "title": "结果、成本与归因",
        "question": "收益来自策略、暴露、单只证券、交易还是现金？",
        "needs": ["完整持仓与价格", "基准/因子收益和归因方法", "费用和公司行动"],
        "basis": "逐股会计贡献不是因果 alpha；Brinson/因子归因需匹配口径。",
    },
    {
        "id": "validation", "section": "结果", "title": "回测可信度与稳健性",
        "question": "结果是否受到前视、实现差异、过拟合或多重比较影响？",
        "needs": ["可用时点和数据谱系", "引擎/代码/参数版本", "独立留出或前向记录"],
        "basis": "回测引擎实现风险和回测过拟合都可能改变结论。",
    },
    {
        "id": "decision_governance", "section": "治理", "title": "授权、例外与版本留痕",
        "question": "谁能改规则，越界如何处理，建议能否回放？",
        "needs": ["硬约束与可调偏好", "版本、审批和例外记录", "输入-输出-成交证据链"],
        "basis": "治理决定谁可以改变什么，以及在什么证据条件下改变。",
    },
)


def coverage(bundle, result):
    """Describe observed quantities separately from unimplemented policy tests."""
    from collections import Counter

    modules = result["modules"]
    account, attr, gov = (modules[k] for k in ("accounting", "attribution", "governance"))
    snapshots = bundle["snapshots"]
    equity = {s["date"]: s["equity"] for s in snapshots}
    counts = Counter(p["date"] for p in bundle["positions"])
    weights = [p["market_value"] / equity[p["date"]] for p in bundle["positions"] if equity.get(p["date"], 0) > 0]
    fills = bundle["fills"]
    turns = [d["gross_turnover"] for d in account["daily"] if d["gross_turnover"] is not None]
    candidates = bundle.get("code", {}).get("candidates", [])
    problems = sum(s["failures"] for s in account["summary"] if s["key"] != "prior_inventory_for_sales")

    def metric(label, value, unit="count"):
        return {"label": label, "value": value, "unit": unit}

    facts = {
        "signal_contract": [metric("代码字面量候选（未确认生效）", len(candidates))],
        "universe": [metric("已交易或持有的证券（不是完整股票池）", len({p["code"] for p in fills + bundle["positions"]}))],
        "portfolio_mapping": [metric("最少收盘持仓", min(counts.get(s["date"], 0) for s in snapshots)), metric("最多收盘持仓", max(counts.get(s["date"], 0) for s in snapshots))],
        "constraint_tradeoffs": [metric("已确认单票上限", gov["settings"]["max_weight"], "ratio"), metric("收盘超限记录", gov["items"][0]["observed"])],
        "risk_exposure": [metric("最大收盘单票权重", max(weights) if weights else None, "ratio")],
        "rebalance": [metric("有成交的日期", len({f["date"] for f in fills})), metric("日均买卖金额 / 前期权益", sum(turns) / len(turns) if turns else None, "ratio")],
        "signal_age": [],
        "costs": [metric("全部导出成交的实际费用", sum(f["fee"] for f in fills), "CNY"), metric("最低佣金溢价情景", gov["scenarios"][0]["value"], "CNY")],
        "capacity": [],
        "completion": [],
        "execution": [metric("已成交笔数", len(fills)), metric("错误级日志条数（不是未成交订单数）", gov["log_errors"])],
        "account_integrity": [metric("账户快照", len(snapshots)), metric("已识别公司行动", len(bundle["actions"])), metric("账目核对异常", problems)],
        "attribution": [metric("区间逐股净贡献合计" if attr["status"] == "verified" else "暂算逐股净贡献合计（未核对）", attr["net_pnl"], "CNY"), metric("最大盈利股 / 全部正贡献", attr["top1_positive_share"], "ratio")],
        "validation": [metric("来源文件摘要", len(bundle["sources"]))],
        "decision_governance": [metric("确认后的审计设置", len(gov["settings"]))],
    }
    risk, cost, behavior = (modules.get(k) for k in ("risk_analysis", "fee_analysis", "trading_behavior"))
    tick, holding = (modules.get(k) for k in ("tick_analysis", "holding_analysis"))
    if risk:
        facts["risk_exposure"] += [metric("股票部分最小有效持仓数", risk["min_effective_names"], "names"),
            metric("快照平均现金权重", risk["mean_cash_weight"], "ratio"),
            metric("最大回撤", risk["max_drawdown"]["ratio"], "ratio")]
        facts["constraint_tradeoffs"].append(metric("期末压回上限的减持金额下界（费用前）", risk["cap_repair"]["marked_sale_lower_bound"], "CNY"))
        facts["attribution"].append(metric("最大回撤期间已扣费用", risk["max_drawdown"]["fees"], "CNY"))
    if cost:
        facts["costs"] += [metric("已定价成交：报告费用减模型费用", cost["reported_minus_modeled"], "CNY"),
            metric("同刻同向合单条件节费", cost["conditional_batch_saving"], "CNY"),
            metric("最低佣金订单金额拐点", cost["breakpoint_notional"], "CNY")]
    if behavior:
        facts["rebalance"].append(metric("同证券同日双向重叠金额占比", behavior["two_way_share"], "ratio"))
    if tick:
        facts["costs"] += [metric("半档价位代理（全部成交）", tick["all_exported_fills"]["weighted_half_spread_bp"], "bp"),
            metric("半档情景成本（已归因区间）", tick["accounted_intervals"]["tick_scenario_cost"], "CNY")]
    if holding:
        facts["rebalance"] += [metric("已平仓股数加权持有自然日", holding["quantity_weighted_calendar_days"], "days"),
            metric("已平仓FIFO片段", holding["closed_summary"]["matches"])]
        facts["attribution"].append(metric("暂不做批次归因的成交", holding["coverage"]["excluded_fill_count"]))
    observations = {
        "signal_contract": "仅识别代码中的参数候选；没有把候选、成交名单或排名当作完整预测流。",
        "universe": "可统计实际出现的证券；无法从已成交样本恢复未入选证券和当时的准入状态。",
        "portfolio_mapping": "已测收盘持仓宽度；代码中的 stock_count 不自动解释为总持仓上限。",
        "constraint_tradeoffs": "已按确认上限核对收盘仓位；当前账户方案可继续比较整手、现金、锁仓和禁买约束。",
        "risk_exposure": "已测单票集中度；尚未计算行业/风格、组合风险和压力情景。",
        "rebalance": "已测成交日和换手；尚未据此推断设计调仓周期，也未验证部分调整的历史效果。",
        "signal_age": "未导入带时间戳的预测历史；有效期算法尚未接入。",
        "costs": "实际费用来自成交表；最低佣金差是原成交不变的情景，不是可实现节省。STRUCT 尚未接入本报告。",
        "capacity": "没有历史参与率分母或盘口供给，容量算法尚未接入。",
        "completion": "没有目标订单与逐日供给，完成期限和受阻后续安排尚未计算。",
        "execution": "已有成交与日志计数；缺订单目标、撤单、部分成交时不能判定漏调、拖延或完整 T+1 合规。",
        "account_integrity": "已核对导出范围内库存、现金、估值和盈亏桥；完整账户生命周期未被认证。",
        "attribution": "已提供逐股会计贡献；策略、风格与构建的因果分解尚未完成。",
        "validation": "已有来源摘要与代码静态解析；未执行代码、切片复跑或多重比较检验。",
        "decision_governance": "输入和结果版本可核对；当前确认只保存审计与计划，不代表策略已启用或已经成交。",
    }
    if risk:
        observations["risk_exposure"] = "已计算权益路径最大回撤、现金比例、股票部分 HHI 和有效持仓数；未将现金当作分散化，也未估计未提供的行业或风格风险。"
        observations["attribution"] = "已核算逐股全期贡献和最大回撤期间的股票/费用贡献；没有基准与反事实时，不把会计贡献命名为策略 alpha。"
    if cost:
        observations["costs"] = "已接逐笔佣金和历史印花税核验、同刻同向合单条件节费、价格不利变动预算和固定成交额外成本压力情景。STRUCT 的历史校准输入尚缺。"
    if behavior:
        observations["rebalance"] = "已测换手和同证券同日双向交易重叠；双向交易可能合理，不将其全部认定为可避免换手。历史目标缺失时不从成交反造目标。"
    if tick:
        observations["costs"] = "已接 STRUCT 一档地板分量的半档情景、价格分组、收益覆盖与可选日均L2报价核对；不认定为实盘成本下限。完整流动性分量尚未接入。"
    if holding:
        observations["rebalance"] += " 已按FIFO重建实际持有期与毛净收益，未平仓右删失及公司行动覆盖缺口分别披露；不把hold_days字面量当强制退出规则。"
    actions = {
        "signal_contract": ("确认信号语义、可用时点、原构建规则，再冻结为审计输入。", "不修改用户预测；补充目标数据后才能测映射偏离。"),
        "universe": ("比较原过滤与可交易过滤，量化被排除目标与遗留暴露；有因子定义时另做涨跌停规则敏感性。", "收窄股票池可能增加集中度；规则敏感性不自动等同于因子污染。"),
        "portfolio_mapping": ("比较现有权重、等权、持仓宽度与单票上限。", "分别列目标偏离、现金占用、集中度与净表现。"),
        "constraint_tradeoffs": ("进入账户方案，求满足现金、整手、可卖数量和交易限制的候选。", "若不可行，列冲突；改动硬约束必须另行确认，不自动放宽。"),
        "risk_exposure": ("比较分散化、行业/风格约束和风险预算候选。", "降风险可能增加偏离或牺牲收益，必须分账展示。"),
        "rebalance": ("比较满调、部分调整、免交易带和不同复核节奏。", "成本、风险、现金暴露和目标跟踪同时比较，不能把少交易直接当优势。"),
        "signal_age": ("比较按年龄复核、保留、替换与分批更新。", "年龄曲线需要冻结信号，不能把多期限 IC 当作同一信号的老化曲线。"),
        "costs": ("比较同一决策批次合单与容忍范围内减少微调。", "等待与合单可能带来价格风险；税费硬规则不作为可优化开关。"),
        "capacity": ("比较规模、参与预算和现金窗口下的目标可达金额。", "缩小规模和延迟会改变暴露；平均参与率不能当成可完成保证。"),
        "completion": ("生成可卖部分、受阻部分及情景下的最早/最晚完成区间。", "同时报告未完成金额与遗留风险；没有供给情景就不报确定日期。"),
        "execution": ("对齐计划与实际，区分策略改变、人工干预和交易失败。", "有目标与订单时才计实现短缺、延迟和机会成本。"),
        "account_integrity": ("先补齐缺失公司行动或现金流水，再重跑相同核对。", "对账错误不通过修改费率、估值或持仓来消除。"),
        "attribution": ("先看逐股集中度，再做基准/风格、构建与执行的分层归因。", "四格或 Shapley 需要可行反事实；不能把单项增量相加当总改善。"),
        "validation": ("冻结少量候选，在共同日期与账户上配对复验，保留独立验证段。", "登记所有尝试和失败窗口；不打开封存数据来凑结果。"),
        "decision_governance": ("把硬约束、可调偏好、人工例外和复核条件分别留痕。", "每条推荐绑定输入、版本、基线和用户确认；不自动下单。"),
    }
    cards = []
    def algorithm(name, implementation="connected", inputs="ready", missing=()):
        return {"name": name, "implementation": implementation, "inputs": inputs, "missing": list(missing)}

    reconciled = "ready" if account["status"] == "verified" else "needs_reconciliation"
    algorithms = {
        "signal_contract": [algorithm("代码参数静态识别", inputs="needs_confirmation"), algorithm("历史信号契约核验", "not_implemented", "missing", STAGES[0]["needs"])],
        "universe": [algorithm("已出现证券统计"), algorithm("时点准入与规则敏感性", "not_implemented", "missing", STAGES[1]["needs"])],
        "portfolio_mapping": [algorithm("收盘持仓宽度与权重"), algorithm("构建政策历史对照", "not_implemented", "missing", ["历史目标与构建公式", "全候选池行情及可交易状态"])],
        "constraint_tradeoffs": [algorithm("收盘上限核验及必要减持下界" if risk else "收盘上限核验"), algorithm("整数账户方案（转入规划）", "available", "needs_confirmation", ["当前目标、可卖数量和行情", "现金、整手、交易限制与硬规则"])],
        "risk_exposure": [algorithm("HHI、有效持仓数、现金与回撤", "connected" if risk else "not_connected", reconciled), algorithm("行业/风格风险归因", "not_implemented", "missing", ["时点暴露与基准/因子收益"])],
        "rebalance": [algorithm("换手与双向交易诊断", "connected" if behavior else "not_connected"), algorithm("满调/分次/免交易带历史复验", "not_implemented", "missing", ["历史信号与目标", "全候选池行情、公司行动及可交易状态"])],
        "signal_age": [algorithm("冻结信号年龄曲线", "not_implemented", "missing", STAGES[6]["needs"])],
        "costs": [algorithm("逐笔费用核验与合单情景", "connected" if cost else "not_connected"), algorithm("完整STRUCT流动性分量", "not_connected", "missing", ["历史原始行情和上月校准特征", "适用域与成本模型选择"])],
        "capacity": [algorithm("参与预算与可达金额", "not_implemented", "missing", STAGES[8]["needs"])],
        "completion": [algorithm("情景供给下完成期限", "not_implemented", "missing", STAGES[9]["needs"])],
        "execution": [algorithm("库存必要条件与成交核验", inputs=reconciled),
                      algorithm("成交时点敏感性", inputs="ready" if (modules.get("fill_timing") or {}).get("status") in ("sensitive", "stable", "unscaled")
                                else "missing", missing=[] if (modules.get("fill_timing") or {}).get("status") in ("sensitive", "stable", "unscaled")
                                else ["成交时间与成交日分钟价（date, time, code, close）"]),
                      algorithm("部分成交与补单", inputs="ready" if modules.get("partial_entry") else "missing",
                                missing=[] if modules.get("partial_entry") else ["委托记录（下单数与成交数）"]),
                      algorithm("实现短缺及延迟分解", "not_implemented", "missing", ["带时点的目标订单、决策价及完整生命周期"])],
        "account_integrity": [algorithm("库存、现金、公司行动与盈亏桥", inputs=reconciled)],
        "attribution": [algorithm("逐股及回撤期会计贡献", inputs=reconciled), algorithm("基准/风格与可行反事实分解", "not_implemented", "missing", ["时点基准、因子收益及构建目标历史"])],
        "validation": [algorithm("输入和结果摘要核对"), algorithm("历史政策配对复验", "not_implemented", "missing", ["共同日期的冻结候选路径", "尝试登记和独立验证段"])],
        "decision_governance": [algorithm("只读导入与追加报告留痕"), algorithm("计划到实际成交闭环", "not_implemented", "missing", ["计划版本、审批和成交回填关联"])],
    }
    if tick:
        algorithms["costs"].extend([algorithm("STRUCT一档地板分量与收益覆盖情景", inputs=reconciled),
            algorithm("同证券同日期L2日均报价核对", inputs="ready" if tick["calibration"]["matched_fills"] else "missing",
                missing=[] if tick["calibration"]["matched_fills"] else ["日均报价观测表；不替代成交时点盘口"])])
    if holding:
        algorithms["rebalance"].append(algorithm("FIFO实际持有期分组（含右删失）", inputs=reconciled))
        algorithms["attribution"].append(algorithm("按买入价及实际持有期的批次归因", inputs=reconciled))
    for stage in STAGES:
        key = stage["id"]
        status = "partial" if facts[key] else "not_implemented"
        if key == "account_integrity":
            status = "verified" if account["status"] == "verified" else "review"
        action, tradeoff = actions[key]
        cards.append({**stage, "status": status, "metrics": facts[key], "observation": observations[key],
                      "candidate_action": action, "tradeoff": tradeoff,
                      "algorithms": algorithms[key],
                      "evidence_scope": "export_observation_and_declared_scenario",
                      "optimization_status": "conditional_fee_scenario" if key == "costs" and cost else "not_replayed"})
    return cards
