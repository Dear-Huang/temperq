"""Evidence-based next steps; a signal score alone never authorizes capital deployment."""

from __future__ import annotations


def _gate(name, status, observed, required, action):
    return {"id": name, "status": status, "observed": observed,
            "required": required, "next_action": action}


def assess(evaluation, diagnostics, evidence, protocol, economic=None):
    ev, d = evaluation, diagnostics
    threshold = protocol["signal_threshold"]
    gain_threshold = protocol["assessment"]["min_anchor_gain_ic"]
    model = ev["models"]["candidate"]
    signal_ci = model["ci95"]
    anchor = ev["paired_differences"]["lgbm_anchor_v1"]
    anchor_ci = anchor["simultaneous_ci95"]
    gates = [
        _gate("comparable_input", "PASS" if ev["comparable"] else "FAIL",
              {"coverage": ev["coverage"], "issues": ev["issues"]},
              "完整覆盖、可定义 IC、候选和参考折声明符合协议",
              "先修复缺失预测、时点或训练折证据；不要通过删除日期改善覆盖。"),
        _gate("positive_signal", ev["signal"]["status"],
              {"score": model["signal_score"], "ci95_score": model["score_ci95"]},
              {"ci_lower_score_strictly_greater_than": 100 * threshold},
              "若区间跨门槛，需要额外独立样本或在开发期改进信号；仅增加重采样次数不能增加市场证据。"),
    ]
    sensitive_ok = all(v["conclusion"]["status"] == "PASS" for v in d["block_sensitivity"])
    gates.append(_gate("block_length_sensitivity", "PASS" if sensitive_ok else "INCONCLUSIVE",
                       d["block_sensitivity"], "所有预设块长下的信号区间下界均高于门槛（开发稳健性检查）",
                       "保留全部块长结果；扩大独立时间覆盖，不能按结果挑选最有利块长。"))
    if not ev["comparable"]:
        incremental = "NOT_EVALUATED"
    elif anchor_ci is None:
        incremental = "INCONCLUSIVE"
    else:
        incremental = ("PASS" if anchor_ci[0] > gain_threshold else
                       "FAIL" if anchor_ci[1] <= gain_threshold else "INCONCLUSIVE")
    gates.append(_gate("incremental_over_anchor", incremental,
                       {"score_gain": None if anchor["mean"] is None else 100 * anchor["mean"],
                        "simultaneous_ci95_score_gain": None if anchor_ci is None else [100 * v for v in anchor_ci]},
                       {"ci_lower_score_gain_strictly_greater_than": 100 * gain_threshold,
                        "family_size": protocol["assessment"]["comparison_family_size"]},
                       "与锚点持平可继续研究，但不能宣称新版更好；按同协议增加独立评测，不反复挑最高分。"))
    for name, required, action in (
        ("net_account", "独立自融资账户、实际成交约束、扣成本净超额及风险预算",
         "补齐账户回放、涨跌停/停牌、费用、现金与仓位对账；毛标签收益不能替代净收益。"),
        ("execution_stress", "预注册费用/滑点/冲击、延迟成交压力测试与资金容量约束",
         "以目标资金规模评估成交比例、成本和压力情景；不能从 IC 推断可管理资金量。"),
        ("reproduction", "模型加载、原始输入到预测重算、独立环境复现",
         "从冻结模型重算预测并比对，在第二环境复现；当前仅检查已有产物与折声明。"),
        ("trial_selection", "完整训练/搜索/失败记录与跨完整工作流重复的不确定性",
         "记录所有试验，分离选择区间与确认区间；缺全体候选时不能估计 DSR/PBO 并冒充已校正。"),
        ("forward_confirmation", "冻结配置后的时间戳预测与前向账户对账",
         "预注册配置、目标和停止规则，累计未来预测和成交；历史高分不能替代未来证据。"),
    ):
        gates.append(_gate(name, "NOT_EVALUATED", None, required, action))
    ledger = economic is not None and economic["status"] == "COMPLETED"
    net_gate = next(g for g in gates if g["id"] == "net_account")
    if ledger:
        net = economic["results"]["extra_0bp_per_side"]["candidate"]
        net_gate.update(status=(net["net_excess_conclusion"]["status"] if
            net["qualification"] == "CONDITIONAL_RESEARCH" and ev["comparable"] else "INCONCLUSIVE"),
            observed=net,
            required="共同研究账户下净超额区间下界严格 > 0，且无陈旧估值、输入合格；不等于实盘准入",
            next_action="结合逐笔拒单、费用、日度回撤定位问题，再补冲击/容量、风险预算及前向成交验证。")
        stress_gate = next(g for g in gates if g["id"] == "execution_stress")
        stress_gate.update(status="INCONCLUSIVE", observed={k: v["candidate"] for k, v in economic["results"].items()},
                           next_action="已测额外单边 10/25 bp；仍需目标资金规模下的冲击、延迟和实际成交证据。")
    issues = []

    def add(code, severity, title, observation, target, action, *, metric=None, shortfall=None):
        locations = (["daily_metrics.parquet", "scorecard.json"] if code.startswith(("SIGNAL", "ANCHOR"))
                     else ["diagnostics.json", "diagnostic_paths.parquet"])
        if "training" in code or "evidence" in code:
            locations += ["原运行目录/folds.json", "原运行目录/task.yaml"]
        issues.append({"id": code, "severity": severity, "title": title,
                       "observation": observation, "target": target, "next_action": action,
                       "metric": metric, "shortfall": shortfall, "locations": locations,
                       "interpretation": "observed_symptom_or_missing_evidence_not_a_proven_cause"})

    for code in ev["issues"]:
        add(code, "BLOCKER", "评分资格未满足", code, "完整协议输入与证据",
            "对照 daily_metrics 的缺失日期与 folds.json 修复对应输入；保持评价样本固定。")
    if ev["signal"]["status"] != "PASS":
        add("SIGNAL_NOT_CONFIRMED", "BLOCKER", "预测能力尚未达到预设证据门槛",
            {"mean_score": model["signal_score"], "ci95_score": model["score_ci95"]},
            {"ci_lower_score_gt": threshold * 100},
            "检查分期和流动性切片；在开发数据中验证改进，再保留独立确认期。",
            metric="signal_score_ci_lower", shortfall=None if signal_ci is None else
            max(0.0, (threshold - signal_ci[0]) * 100))
    if incremental != "PASS":
        add("ANCHOR_INCREMENT_NOT_CONFIRMED", "INFO", "未证明比冻结锚点更好",
            {"gain_score": None if anchor["mean"] is None else 100 * anchor["mean"],
             "simultaneous_ci95": None if anchor_ci is None else [100 * v for v in anchor_ci]},
            {"ci_lower_gain_score_gt": gain_threshold * 100},
            "持平不是训练失败；改进声明需满足预注册最小增量，并用独立证据确认。",
            metric="anchor_gain_ci_lower", shortfall=None if anchor_ci is None else
            max(0.0, (gain_threshold - anchor_ci[0]) * 100))
    if not sensitive_ok:
        add("BLOCK_SENSITIVITY", "WARNING", "结论依赖块长或样本不足",
            d["block_sensitivity"], "全部预设块长检查", gates[2]["next_action"])
    negative = [v for v in d["slices"] if v["mean"] is not None and v["mean"] <= 0]
    if negative:
        add("WEAK_SLICES", "WARNING", "部分时期或流动性分组的预测排序没有正向均值",
            [{k: v[k] for k in ("dimension", "name", "n", "mean", "ci95")} for v in negative],
            "探索性定位；不要求每个短切片显著，也不据此认定因果",
            "在开发区间复核对应分组与特征；不要删除不利测试切片后重新排名。")
    gaps = d["data_gaps"]
    if gaps["missing_mature_label_pairs"]:
        add("MATURE_LABEL_GAPS", "WARNING", "有日历上已成熟但缺失收益的样本被排除",
            gaps, "审计退市、停牌、复权或行情缺口的标签处理",
            "核对原始证券和行情记录；当前可用标签分数不能代表缺失部分，不能用零收益填补。")
    gross = d["cohort_economics"]
    low_share = gross["mean_top_low_liquidity_share"]
    if low_share is not None and low_share > 0.5:
        add("LOW_LIQUIDITY_CONCENTRATION", "WARNING", "买入端大多数股票来自低成交额组",
            {"top_low_liquidity_share": low_share,
             "universe_low_liquidity_share": gross["mean_universe_low_liquidity_share"]},
            "集中度是描述性诊断，不是资金容量或流动性安全阈值",
            "在开发期预注册高流动性股票池对照，检查信号与收益是否依赖低成交额股票，再做目标资金规模的冲击和成交测试。")
    weak_cost = [v for v in gross["cost_scenarios"] if v["diagnostic_conclusion"]["status"] != "PASS"]
    if weak_cost:
        add("THIN_GROSS_COST_MARGIN", "WARNING", "前排股票的毛收益空间尚不能覆盖部分假设摩擦门槛",
            weak_cost, "事件分析诊断，不是净账户验收",
            "检查高排名信号的收益集中度，补实际费用与成交回放；不要把假设残差称为净收益。")
    if ledger:
        if net_gate["status"] != "PASS":
            add("NET_EXCESS_NOT_CONFIRMED", "BLOCKER", "研究账户尚未确认扣成本后优于可实现等权",
                net, net_gate["required"], net_gate["next_action"])
        add("ACCOUNT_EXECUTION_LIMITS", "BLOCKER", "已有研究净账户，但实用成交与风险证据仍不完整",
            {"limits": economic["limits"]}, "目标资金规模、风险预算、真实执行与退市/公司行动记录",
            "核对陈旧估值、期末持仓和拒单；补开盘流动性、容量/冲击、整数手及前向成交验证。")
        if any(v["candidate"]["net_excess_conclusion"]["status"] != "PASS" for v in economic["results"].values()):
            add("NET_COST_STRESS", "WARNING", "部分实际账户成本情景未确认正向净超额",
                {k: v["candidate"] for k, v in economic["results"].items()},
                "同协议额外单边滑点情景；属于条件研究，不是市场真实冲击估计",
                "结合换手与买入端流动性，在开发期检验降低换手、组合缓冲区或更可交易的股票池。")
    else:
        add("ECONOMICS_UNVERIFIED", "BLOCKER", "缺少投入实用所需的净账户与成交证据",
            {"cohort_status": gross["status"], "account_reason": (economic or {}).get("reason")},
            net_gate["required"], net_gate["next_action"])
    for f in issues:
        if f["id"] in {"NET_EXCESS_NOT_CONFIRMED", "ACCOUNT_EXECUTION_LIMITS", "NET_COST_STRESS"}:
            f["locations"] = ["economics.json", "account_paths.parquet", "fills.parquet", "holdings.parquet"]
    add("FORWARD_AND_SELECTION_UNVERIFIED", "BLOCKER", "历史分数尚未得到独立前向和完整试验确认",
        {"sample_status": evidence["sample_status"], "declared_test_views": evidence.get("declared_test_views")},
        "冻结后前向记录、完整试验历史和独立复现", gates[-1]["next_action"])
    if not ev["comparable"]:
        level, message = "REPAIR_INPUT", "先修复评分输入或证据；当前成绩仅供诊断。"
    elif ev["signal"]["status"] == "PASS" and sensitive_ok:
        level, message = "SIGNAL_RESEARCH_CANDIDATE", "已有历史正向信号证据，可进入组合与成交验证研究；尚不能据此投入实盘。"
    else:
        level, message = "RESEARCH_ONLY", "当前证据只支持继续研究，尚未确认稳定正向信号。"
    return {"level": level, "message": message, "gates": gates, "findings": issues,
            "paper_trading_eligible": False, "live_capital_eligible": False,
            "incremental_claim": incremental,
            "threshold_semantics": "严格超过区间下界门槛；shortfall 是当前区间下界差距，不是保证达标所需的训练提分。",
            "no_universal_live_score": True,
            "policy_source": "QW development assessment policy, not an academic universal deployment standard"}
