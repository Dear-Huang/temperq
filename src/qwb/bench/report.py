"""Chinese user reports generated from the same objects as machine-readable output."""

from __future__ import annotations


def number(value, digits=4):
    return "N/A" if value is None else f"{value:+.{digits}f}"


def interval(value):
    return "N/A" if value is None else f"[{value[0]:+.4f}, {value[1]:+.4f}]"


LABELS = {"PASS": "通过", "FAIL": "未通过", "INCONCLUSIVE": "证据不足",
          "NOT_EVALUATED": "未评估", "INVALID": "无效"}
GATE_NAMES = {"comparable_input": "输入与比较资格", "positive_signal": "历史正向信号",
              "block_length_sensitivity": "块长敏感性", "incremental_over_anchor": "优于冻结锚点",
              "net_account": "扣成本账户", "execution_stress": "执行和容量压力",
              "reproduction": "预测重算与复现", "trial_selection": "试验选择偏差",
              "forward_confirmation": "冻结后前向确认"}
SLICE_NAMES = {"year": "年份", "quarter": "季度", "liquidity": "流动性",
               "market_history": "过去市场状态", "low": "低", "middle": "中", "high": "高",
               "trailing_market_positive": "过去20日市场均值为正",
               "trailing_market_nonpositive": "过去20日市场均值非正"}


def finding_observation(f):
    code, value = f["id"], f["observation"]
    if code == "SIGNAL_NOT_CONFIRMED":
        return [f"IC 分 {number(value['mean_score'], 2)}，95% 区间 {interval(value['ci95_score'])}。"]
    if code == "ANCHOR_INCREMENT_NOT_CONFIRMED":
        return [f"相对锚点 {number(value['gain_score'], 2)} IC 分；校正区间 {interval(value['simultaneous_ci95'])}。"]
    if code == "MATURE_LABEL_GAPS":
        return [f"日历已成熟的股票日样本 {value['mature_universe_pairs']:,} 个，其中 "
                f"{value['missing_mature_label_pairs']:,} 个缺少收益，涉及 {value['missing_mature_label_dates']} 个日期。",
                "具体股票/日期示例见 `diagnostics.json` 的 `data_gaps.missing_mature_label_examples`。"]
    if code == "LOW_LIQUIDITY_CONCENTRATION":
        base = value["universe_low_liquidity_share"]
        base_text = "N/A" if base is None else f"{base:.1%}"
        return [f"前排股票的低成交额组平均占比 {value['top_low_liquidity_share']:.1%}；"
                f"同期评价股票池占比 {base_text}。分组依据过去 20 日成交额，不等同于市值分组。"]
    if code == "THIN_GROSS_COST_MARGIN":
        return [f"假设额外摩擦 {v['incremental_round_trip_bp']} bp：剩余毛空间 "
                f"{number(v['mean_residual_bp'], 2)} bp，区间 {interval(v['ci95_residual_bp'])} bp，"
                f"{LABELS[v['diagnostic_conclusion']['status']]}。" for v in value]
    if code == "BLOCK_SENSITIVITY":
        return [f"块长 {v['block_days']} 日：{LABELS[v['conclusion']['status']]}，"
                f"IC 区间 {interval(v['ci95'])}。" for v in value]
    if code == "WEAK_SLICES":
        return [f"{SLICE_NAMES[v['dimension']]} {SLICE_NAMES.get(v['name'], v['name'])}："
                f"IC {number(v['mean'])}，有效日 {v['n']}，区间 {interval(v['ci95'])}。" for v in value]
    if code == "ECONOMICS_UNVERIFIED":
        return ["目前没有可用的独立扣成本账户结果。", f"账户未评估原因：{value.get('account_reason')}。"]
    if code == "NET_EXCESS_NOT_CONFIRMED":
        return [f"年化算术净超额 {number(value['net_excess_ann_bp'], 2)} bp；"
                f"95% 区间 {interval(value['net_excess_ann_ci95_bp'])} bp；估值资格 {value['qualification']}。",
                f"陈旧收盘估值 {value['stale_close_name_days']} 股票日；调仓时陈旧开盘估值 {value['stale_open_name_days']} 股票次。"]
    if code == "ACCOUNT_EXECUTION_LIMITS":
        return value["limits"]
    if code == "NET_COST_STRESS":
        return [f"{k}：年化算术净超额 {number(v['net_excess_ann_bp'], 2)} bp，"
                f"区间 {interval(v['net_excess_ann_ci95_bp'])} bp。" for k, v in value.items()]
    if code == "FORWARD_AND_SELECTION_UNVERIFIED":
        return ["当前为本地自报历史折外预测；没有冻结后独立前向证据或完整搜索校正。",
                f"旧运行报告自报的测试查看次数：{value['declared_test_views']}，该记录不能认证外部试验。"]
    return [str(value)]


def readiness_section(s):
    r = s["readiness"]
    ev = s["evaluation"]
    ci = ev["paired_differences"]["lgbm_anchor_v1"]["simultaneous_ci95"]
    gain = ev["paired_differences"]["lgbm_anchor_v1"]["mean"]
    lines = ["## 可以用到哪一步", "", f"**{r['message']}**", "",
             "不存在一个通用的“IC 分超过 X 就可实盘”门槛。相同 IC 可能对应完全不同的换手、成本和风险。",
             "当前未开放仿真准入或实盘准入认证；可以进行研究性仿真开发，但不能将历史评分视作已经通过。", "",
             f"相对冻结 LightGBM 的 IC 分差：{number(None if gain is None else gain * 100, 2)}；",
             f"四参考比较校正后的 95% 分差区间：{interval(None if ci is None else [v * 100 for v in ci])}。", "",
             "| 检查 | 结果 | 达标条件 |", "|---|---|---|"]
    for gate in r["gates"]:
        target = gate["required"]
        if isinstance(target, dict):
            if gate["id"] == "positive_signal":
                target = f"IC 分区间下界严格 > {target['ci_lower_score_strictly_greater_than']:g}"
            else:
                target = f"相对锚点分差的校正区间下界严格 > {target['ci_lower_score_gain_strictly_greater_than']:g}"
        lines.append(f"| {GATE_NAMES[gate['id']]} | {LABELS[gate['status']]} | {target} |")
    lines += ["", "锚点增量是版本改进检查；与锚点持平不代表训练失败。其余实用证据不能用更高 IC 分抵消。", "",
              "## 短板、证据与下一步", ""]
    for f in r["findings"]:
        target = f["target"]
        if isinstance(target, dict):
            metric = "IC 分" if f["id"] == "SIGNAL_NOT_CONFIRMED" else "相对锚点 IC 分差"
            target = f"{metric}区间下界严格 > {next(iter(target.values())):g}"
        lines += [f"### {f['title']}", "", f"证据编号：`{f['id']}`；级别：`{f['severity']}`。", "",
                  *finding_observation(f), "", f"条件：{target}。", "",
                  f"下一步：{f['next_action']}", "",
                  "定位文件：" + "、".join(f"`{p}`" for p in f["locations"]) + "。", ""]
        if f["shortfall"] is not None:
            lines += [f"当前区间下界距离门槛 {f['shortfall']:.4f} IC 分，且必须严格超过。"
                      "这是当前证据差距，不是保证达标所需的训练提分。", ""]
    lines += ["以上定位描述观测到的短板或缺失证据，不声称已经找到模型失效的因果原因。", ""]
    return lines


def diagnostics_section(s):
    d = s["diagnostics"]
    c = d["cohort_economics"]
    lines = ["## 前排股票的毛收益空间", "",
             f"逐日选择预测前 10%，观察其未来 {c['horizon_days']} 个交易日标签收益。每日股票篮子可能重叠；",
             "这不是自融资账户，没有模拟涨跌停、实际成交、持仓、退市、冲击或资金容量，不作年化。", "",
             f"前 10% 相对当日合格股票等权的毛收益差：{number(c['mean_top_minus_universe_gross_bp'], 2)} bp/标签期；",
             f"95% 区间：{interval(c['ci95_top_minus_universe_gross_bp'])} bp。", "",
             "以下假设前排股票篮子比无摩擦等权比较组多付一次完整买卖摩擦，检查剩余空间。",
             "10/30/60 bp 是协议内的诊断情景，不是估计的市场实际费用，也不是实用门槛。", "",
             "| 假设额外买卖摩擦 | 剩余空间均值 | 95% 区间 | 剩余空间为正？ |",
             "|---|---:|---|---|"]
    for item in c["cost_scenarios"]:
        lines.append(f"| {item['incremental_round_trip_bp']} bp | {number(item['mean_residual_bp'], 2)} bp | "
                     f"{interval(item['ci95_residual_bp'])} bp | {LABELS[item['diagnostic_conclusion']['status']]} |")
    share = c["mean_top_low_liquidity_share"]
    share_text = "N/A" if share is None else f"{share:.1%}"
    lines += ["", "情景残差不能称为扣成本净收益。高毛收益空间也不证明可实现。",
              f"前排股票中低成交额组平均占比：{share_text}；该比例不是容量估计。", "",
              "| 预测从低到高分组 | 毛标签收益均值 |", "|---|---:|"]
    for i, value in enumerate(c["quintile_mean_gross_bp"], 1):
        lines.append(f"| 第 {i}/5 组 | {number(value, 2)} bp |")
    lines += ["", "分组使用固定股票代码顺序处理并列预测；集中并列的股票篮子结果需要额外审视。", "",
              "## 稳定性与适用范围", "", "| 切片 | 名称 | 有效日 | Rank IC | 探索性95%区间 |",
              "|---|---|---:|---:|---|"]
    for v in d["slices"]:
        lines.append(f"| {SLICE_NAMES[v['dimension']]} | {SLICE_NAMES.get(v['name'], v['name'])} | "
                     f"{v['n']} | {number(v['mean'])} | {interval(v['ci95'])} |")
    lines += ["", "切片使用决策时已知的成交额和过去市场信息；切片结果仅用于定位，不作独立显著性声明。",
              "切片样本不足时区间为空；短期负均值不自动证明模型失效，正均值也不证明稳健。", "",
              "| 日期块长度 | 均值 | 95% 区间 | 正向信号 |", "|---|---:|---|---|"]
    for v in d["block_sensitivity"]:
        lines.append(f"| {v['block_days']} 日 | {number(v['mean'])} | {interval(v['ci95'])} | {LABELS[v['conclusion']['status']]} |")
    lines += ["", "## 可复查的证据", "",
              "`daily_metrics.parquet` 保存逐日 IC、覆盖和排除记录；`diagnostic_paths.parquet` 保存股票篮子毛标签收益和流动性切片路径。",
              "`diagnostics.json`、`readiness.json` 与本报告由同一计算结果生成；`checksums.json` 固定本地文件哈希。",
              "`bench verify <结果目录> --inputs` 可检查文件和原始输入哈希，并从逐日路径重算均值；",
              "本地哈希不是第三方认证，也不能证明原始数据或训练自报真实。", "",
              "方法依据与能力边界见项目 `bench/references/PRACTICAL_EVIDENCE_REVIEW.md`；",
              "主要参考 FinTSB（2026 正式论文）、FinVerse（2026 预印本）、FinRL-X（2026 预印本）及金融多重试验文献。", ""]
    return lines


def model_report(s: dict) -> str:
    labels = LABELS
    lines = ["# QW-Bench 模型报告", "", f"开发诊断版 {s['scorer_version']}；不作为正式经济或平台成绩。", "",
             f"任务：`{s['task']}`；区间：{s['evaluation_years']}；数据：`{s['data_snapshot_id']}`。", ""]
    if s["status"] == "INVALID":
        return "\n".join(lines + ["**评估无效，未产生分数或排名。**", "", *s["errors"], "",
                       "先根据错误定位输入：时区/决策时点、重复股票/日期、任务名、快照和文件哈希必须与协议一致。",
                       "行情或冻结参考改变时创建新协议，并在相同协议下重评双方；不要覆盖旧成绩或删除不利样本。", ""])
    ev = s["evaluation"]
    model = ev["models"]["candidate"]
    rank = ev["reference_rank"]
    rank_text = f"第 {rank['position']}/{rank['out_of']}（同分参考 {rank['ties']} 个）" if rank else "不具备排名资格"
    signal = labels[ev["signal"]["status"]]
    lines += [f"**IC 分 {number(model['signal_score'], 2)}；固定参考排名：{rank_text}；预测信号：{signal}。**", "",
              "IC 分 = 100 × 日均 Rank IC，可为负，不是成功概率。排名仅按点估计，不代表显著优于其他模型。", "",
              f"- 预测覆盖率：{ev['coverage']['total']:.2%}；最低每日覆盖：{ev['coverage']['min_daily']:.2%}。",
              f"- 有效 IC 日期：{ev['coverage']['valid_ic_dates']}；预定义排除日期：{ev['coverage']['excluded_dates']}。",
              f"- 训练折声明检查：{labels[s['evidence']['status']]}；样本关系：`{s['evidence']['sample_status']}`。",
              f"- 研究账户回放：{s['economic']['status']}。模型加载、推理复现及训练无泄漏认证：未评估。", ""]
    lines += economics_section(s)
    lines += readiness_section(s)
    lines += ["## 固定参考对照", "", "| 模型 | Rank IC | IC 分 | IC 均值 95% 区间 |",
              "|---|---:|---:|---|"]
    for name, v in ev["models"].items():
        lines.append(f"| {name} | {number(v['mean'])} | {number(v['signal_score'], 2)} | {interval(v['ci95'])} |")
    lines += ["", "## 相对参考的日期配对差值", "", "| 参考 | 候选减参考 IC | 四参考校正95%区间 | 严格优于参考？ |",
              "|---|---:|---|---|"]
    for name, v in ev["paired_differences"].items():
        lines.append(f"| {name} | {number(v['mean'])} | {interval(v['simultaneous_ci95'])} | {labels[v['conclusion']['status']]} |")
    lines += ["", "此表判断差值是否严格大于零；与自身锚点相同不构成训练失败。",
              "区间跨零表示无法确认差异，不等于模型相同；四参考使用 Bonferroni 分位区间近似控制同组多重比较。",
              "它不校正用户此前尝试过的全部模型、特征、参数或反复访问测试集。", "",
              "## 分年表现", "", "| 年份 | 有效日期 | Rank IC |", "|---|---:|---:|"]
    for year, v in ev["by_year"].items():
        lines.append(f"| {year} | {v['n']} | {number(v['mean'])} |")
    lines += [""] + diagnostics_section(s)
    lines += ["", "## 资格与证据边界", ""]
    lines += [f"- {issue}" for issue in ev["issues"] + s["evidence"]["issues"]]
    lines += ["- 来源为本地自报；折声明检查不认证外部试验、特征来源或训练真实性。",
              "- 历史评价区间可能已经多次使用；本次结果不能作为独立前向确认。",
              "- 置信区间仅反映日期不确定性；未评估重复训练与模型选择的不确定性。",
              "- 日历尾部标签未成熟、规则特征预热及不足 20 只股票的日期按协议排除；不按候选输出删样本。",
              f"- 统计设置：`{s['statistics']}`；参数未经开发期校准，协议仍为 development。",
              f"- 协议哈希：`{s['protocol_hash']}`；评分器：`{s['scorer_version']}`。", ""]
    lines += [f"- {v}" for v in s["evidence"]["limits"]]
    return "\n".join(lines) + "\n"


def economics_section(s):
    e = s["economic"]
    lines = ["## 独立扣成本研究账户", ""]
    if e["status"] != "COMPLETED":
        return lines + [f"未评估：{e.get('reason')}。信号诊断仍保留。", ""]
    cfg = e["account_config"]
    lines += ["**此结果受日线成交与分数复权单位假设约束；不含冲击和资金容量，不是实盘认证。**", "",
        f"从 {e['start']} 收盘现金开始，至 {e['end']} 收盘估值；每 {s['diagnostics']['cohort_economics']['horizon_days']} 个交易日决策，次日开盘换仓，共 {e['decision_dates']} 次。",
        "排序策略固定前10%等权；等权参考买全部决策股票池；均先卖后买、禁止透支、未成交留现金。",
        f"共同股票池还要求三个规则参考在决策日有足够历史；因此排除 {e['universe_eligibility']['excluded_missing_past_reference_pairs']:,} 个调仓股票次，不读取未来标签。",
        "跌停/停牌无法卖出时保留原单位数量；所有策略使用相同执行与费用。期末不强制平仓。", "",
        f"初始资金 {cfg['initial_cash']:,.0f}，单边佣金假设 {cfg['commission_bp']:g} bp，加上上月数据估计的 STRUCT 半价差与当日卖出印花税。",
        "额外单边 10/25 bp 滑点作用于候选和等权各自的实际成交；不是毛标签收益直接减常数。", "",
        "经济量 = 252 × 10,000 × 日度净收益差均值，单位年化 bp；100 bp = 1 个百分点。",
        "它是条件研究量，不是正式经济分。CAGR 单列；区间为日期块边际区间，不校正搜索、执行和全部多重比较。", "",
        "| 基础费用下账户 | 总收益 | CAGR | 最大回撤 | 相对可实现等权年化净超额 | 95%区间(bp) | 估值资格 |",
        "|---|---:|---:|---:|---:|---|---|"]
    for name, v in e["results"]["extra_0bp_per_side"].items():
        lines.append(f"| {name} | {v['total_return']:+.2%} | {v['cagr']:+.2%} | {v['max_drawdown']:.2%} | "
                     f"{v['net_excess_ann_bp']:+.2f} bp | {interval(v['net_excess_ann_ci95_bp'])} | {v['qualification']} |")
    lines += ["", "| 候选额外单边滑点 | 年化净超额(bp) | 95%区间(bp) | 数值结论 |", "|---|---:|---|---|"]
    for scenario, values in e["results"].items():
        v = values["candidate"]
        lines.append(f"| {scenario} | {v['net_excess_ann_bp']:+.2f} | {interval(v['net_excess_ann_ci95_bp'])} | "
                     f"{LABELS[v['net_excess_conclusion']['status']]} |")
    v = e["results"]["extra_0bp_per_side"]["candidate"]
    lines += ["", f"候选费用分解：佣金 {v['commission']:,.2f}，印花税 {v['stamp_tax']:,.2f}，价差/滑点 {v['slippage']:,.2f}。",
        f"请求 {v['requested_orders']:,} 笔，零成交 {v['rejected_orders']:,} 笔，现金缩量 {v['scaled_orders']:,} 笔；单边换手日均 {v['mean_one_way_turnover']:.2%}。",
        f"期末现金 {v['ending_cash']:,.2f}，净值 {v['ending_equity']:,.2f}，仍持有 {v['ending_held_names']} 只；净值不等于现金。",
        f"候选陈旧收盘估值 {v['stale_close_name_days']} 股票日、调仓开盘陈旧估值 {v['stale_open_name_days']} 股票次；"
        f"缺价格/涨跌停价/价差估计导致的拒单 {v['data_gap_orders']} 笔；候选或等权出现任一次陈旧估值或这些数据缺口，比较均标 DIAGNOSTIC_ONLY。", "",
        "`account_paths.parquet`、`fills.parquet`、`holdings.parquet` 可核对每天现金、单位、费用和估值；",
        "`bench verify` 会独立累计成交现金流/数量、核对日末持仓和净值，并重算指标及区间。", "",
        *[f"- {x}" for x in e["limits"]], "",
        "| 年份 | 候选区间净收益 | 相对可实现等权年化净超额(bp) |", "|---|---:|---:|"]
    for year, yearly in v["by_year"].items():
        lines.append(f"| {year} | {yearly['net_return']:+.2%} | {yearly['net_excess_ann_bp']:+.2f} |")
    lines += ["", "分年收益按该年已有日收益复利，不重置现金；属于探索性诊断，不独立选最优年份。", ""]
    return lines


def comparison_report(s: dict) -> str:
    delta = s["difference"]
    lines = [
        "# QW-Bench 预测结果比较", "", "开发历史对照；不是平台经济主分或完整训练流程比较。", "",
        f"新减旧 Rank IC：{number(delta['mean'])}；IC 分差：{number(s['signal_score_gain'], 2)}。",
        f"日期配对差值 95% 区间：{interval(delta['ci95'])}；结论：{s['conclusion']['status']}。", "",
        f"结论检查区间下界是否严格超过预注册最小增量 {s['minimum_signal_score_gain']:g} IC 分；持平不是模型训练失败。", "",
        "区间跨零表示证据不足，不表示两者相同。区间未包含重复训练与选择不确定性。",
        f"协议：`{s['protocol_hash']}`；有效配对日期：{delta['n']}。", "",
    ]
    economic = s.get("economic_comparison", {})
    if economic.get("status") == "COMPLETED":
        lines += ["## 同规则净账户比较", "", "新减旧的日净收益配对差，乘 252 × 10,000，单位年化 bp。",
                  "这是有条件研究路径比较，不代表完整平台训练/搜索流程改进或可实盘。未校正全部模型搜索与多情景比较。", "",
                  "| 额外单边滑点情景 | 新减旧年化净增量(bp) | 95%区间(bp) | 严格改善？ |", "|---|---:|---|---|"]
        for scenario, v in economic["results"].items():
            lines.append(f"| {scenario} | {v['annual_net_gain_bp']:+.2f} | {interval(v['ci95_annual_net_gain_bp'])} | {LABELS[v['conclusion']['status']]} |")
        lines += [""]
    return "\n".join(lines)
