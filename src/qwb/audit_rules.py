"""Every rule an audit conclusion rests on, with its kind and source.

Three kinds, never mixed: ``fact`` (exchange and tax rules, accounting identities),
``method`` (a published or documented method), and ``threshold`` (a judgement call
by the project, not yet tested on samples). Only thresholds may be adjusted.
Reports store the values they used, so a changed threshold yields a new report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERSION = "audit_rules_v1"


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str                     # fact | method | threshold
    text: str
    source: str
    value: object = None
    unit: str = ""
    applies_to: tuple = ("stock", "fund", "bond")
    range: tuple | None = None    # thresholds only: (min, max)

    def __post_init__(self):
        if self.kind not in {"fact", "method", "threshold"}:
            raise ValueError(f"{self.id}: unknown rule kind {self.kind}")
        if (self.kind == "threshold") != (self.range is not None):
            raise ValueError(f"{self.id}: thresholds, and only thresholds, need an allowed range")


UNTESTED = "经验阈值，未经样本检验"

RULES = {r.id: r for r in (
    # Facts
    Rule("F-ACCOUNT", "fact", "权益 = 现金 + 持仓市值；持仓变化 = 成交；现金变化 = 成交金额 + 费用", "账务恒等式"),
    Rule("F-TICK-STOCK", "fact", "股票最小报价单位 0.01 元", "沪深交易所交易规则", .01, "元", ("stock",)),
    Rule("F-TICK-FUND", "fact", "ETF、基金、债券最小报价单位 0.001 元", "沪深交易所交易规则", .001, "元", ("fund", "bond")),
    Rule("F-STAMP", "fact", "卖出股票按成交日适用税率缴印花税；2023-08-28 起 0.05%，此前 0.1%；基金和债券免征",
         "财政部、税务总局公告"),
    Rule("F-MIN-COMMISSION", "fact", "佣金不足最低佣金时按最低佣金收取", "券商佣金规则；最低额由合同约定"),
    # Methods
    Rule("M-TICK-HALF", "method", "价差下限 = 每边半个最小价位", "作者 STRUCT 稿（SSRN，未经同行评审）"),
    Rule("M-FIFO", "method", "按先进先出配对买卖，计算每笔已平仓交易的持有期和盈亏", "通用会计配对方法"),
    Rule("M-SLIPPAGE", "method", "回测已计的滑点按平台文档换算为每边成本，价差情景只补差额",
         "聚宽 set_slippage 文档；RQAlpha 滑点模型文档"),
    Rule("M-RANK-IC", "method", "每个调仓日，用全部候选标的的分数与到下一调仓日的收益做秩相关（Spearman IC）",
         "因子检验常用方法（信息系数）"),
    Rule("M-PREMISE-POWER", "method", "最小可检测 IC = (显著性 t 门槛 + 0.84) × IC 标准差 / √期数，0.84 对应八成检验力",
         "统计检验力的正态近似（单侧）"),
    Rule("M-RANK-REPLAY", "method", "用策略导出的每日排名回放换仓次数；不重算收益", "本项目，见 audit_signals"),
    # Thresholds
    Rule("T-HOLD-SHORT", "threshold", "持有不超过这个自然日数算短持有", UNTESTED, 5, "自然日", range=(1, 60)),
    Rule("T-PRICE-LOW", "threshold", "低于这个价格的股票算低价股", UNTESTED, 5., "元", ("stock",), (1., 50.)),
    Rule("T-MIN-COMM-MATERIAL", "threshold", "最低佣金多付超过这个金额才建议处理", UNTESTED, 500., "元", range=(0., 100000.)),
    Rule("T-BUFFER-RANK", "threshold", "保留带：持有的标的排进前几名就不卖", UNTESTED, 2, "名", range=(2, 10)),
    Rule("T-WHIPSAW-DAYS", "threshold", "卖出后这么多个信号日内又排回第一，算来回", UNTESTED, 5, "信号日", range=(1, 60)),
    Rule("T-CHURN-SHARE", "threshold", "保留带少换的次数占换仓次数达到这个比例才建议改", UNTESTED, .2, "比例", range=(0., 1.)),
    Rule("T-CHURN-MIN", "threshold", "保留带至少少换这么多次才建议改", UNTESTED, 3, "次", range=(1, 100)),
    Rule("T-CASH-BUFFER", "threshold", "全仓下单时预留的现金比例", UNTESTED, .01, "比例", range=(.001, .1)),
    Rule("T-CASH-SHORT", "threshold", "拒单所差金额不超过下单金额的这个比例，算“差一点”", UNTESTED, .01, "比例", range=(.001, .1)),
    Rule("T-FEE-GAP-SHARE", "threshold", "费用差额占复算费用达到这个比例才出现费用卡", UNTESTED, .01, "比例", range=(0., .1)),
    Rule("T-DD-TOLERANCE", "threshold", "改版最大回撤比原版多出这么多（百分点）就不算更好", UNTESTED, .01, "比例", range=(0., .2)),
    Rule("T-PREMISE-T", "threshold", "IC 均值的 t 统计量达到这个值，才说前提成立或反了（全期与各分段相同）", UNTESTED, 2., "", range=(1., 5.)),
    Rule("T-PREMISE-MIN-PERIODS", "threshold", "至少这么多个调仓周期才检验前提", UNTESTED, 20, "期", range=(5, 500)),
    Rule("T-TIMING-SHARE", "threshold", "换个成交时点，盈亏变化达到净盈亏的这个比例，就说结论依赖成交时点", UNTESTED, .25, "比例", range=(.01, 1.)),
    Rule("M-FILL-TIMING", "method", "固定原成交股数和费用，把成交价换成同日其他分钟的收盘价，比较盈亏；不重跑策略决策", "本项目，见 audit_timing"),
    Rule("T-FILL-BEST-SHARE", "threshold", "成交价落在当天最有利一成以内的比例达到这个值（且显著高于随机的 10%），就标为可疑", UNTESTED, .25, "比例", range=(.12, .9)),
    Rule("M-FILL-RANGE", "method", "把每笔成交价放进当天最高最低价区间，和均匀分布比较；买在低位、卖在高位的比例显著超过随机即可疑", "本项目，见 audit_fill_quality"),
    Rule("M-FILL-FLOOR", "method", "每笔买入按成交后 10 分钟内的最高价、卖出按最低价重算，股数费用不变；是执行的悲观下限，不是估计", "本项目，见 audit_fill_quality"),
    Rule("T-PARTIAL-DAYS", "threshold", "买单只成交一部分后，连续这么多个交易日没有补单才列出", UNTESTED, 5, "交易日", range=(1, 60)),
    Rule("M-PARTIAL-ENTRY", "method", "按委托记录的下单数和成交数找部分成交的买单，跟踪到该标的下一次委托或成交为止", "本项目，见 audit_completion"),
    Rule("T-FEE-GAP-MIN", "threshold", "费用差额至少这么多元才出现费用卡", UNTESTED, 1., "元", range=(0., 1000.)),
)}


def defaults():
    return {r.id: r.value for r in RULES.values() if r.kind == "threshold"}


def resolve(overrides=None):
    """Threshold values for one audit; facts and methods cannot be overridden."""
    values = defaults()
    for key, value in (overrides or {}).items():
        rule = RULES.get(key)
        if rule is None or rule.kind != "threshold":
            raise ValueError(f"不能调整的规则：{key}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key} 须为数字")
        low, high = rule.range
        if not low <= value <= high:
            raise ValueError(f"{key} 须在 {low}–{high} {rule.unit} 之间")
        values[key] = type(rule.value)(value)
    return values


def snapshot(values):
    """What a report stores: registry version and every threshold value used."""
    return {"version": VERSION, "thresholds": dict(values)}


def cite(*ids):
    """Rule references for a conclusion, in display form."""
    out = []
    for rid in ids:
        rule = RULES[rid]
        out.append({"id": rid, "kind": rule.kind, "text": rule.text, "source": rule.source})
    return out


# Evidence levels shared by every card and check (see docs/specs/AUDIT_EVIDENCE_SPEC.md §3).
EVIDENCE = {
    "E1": "记录事实：导出记录与账务恒等式上的复算",
    "E2": "描述：事后分组，不证明改了会更好",
    "E3": "情景：固定原成交，在声明假设下估计",
    "E3c": "条件情景：有前提未确认，结论随它变",
    "E4": "同区间复跑：同一段历史重跑的比较，仍是样本内",
    "E5": "独立验证段通过：冻结候选后在未看过的验证段通过",
    "E6": "前向观察：模拟盘或实盘记录",
    "N": "待补材料：缺少计算所需材料",
}


KIND_NAMES = {"fact": "规则事实", "method": "有出处的方法", "threshold": "经验阈值"}
ASSET_NAMES = {"stock": "股票", "fund": "ETF/基金", "bond": "债券"}


def _fmt(rule, value):
    if rule.unit == "比例":
        return f"{value:.1%}".replace(".0%", "%")
    return f"{value:g} {rule.unit}".strip() if isinstance(value, (int, float)) else str(value)


def document():
    """docs/AUDIT_RULES.md, generated from RULES; a test keeps the file and the code identical."""
    out = ["# 审计规则登记表", "",
           f"注册表版本：`{VERSION}`。本文件由 `qwb.audit_rules.document()` 生成，请勿手改；"
           "改规则时改代码并重新生成，测试会核对两者一致。", "",
           "每条审计结论都标注它用到的规则编号。三类规则分开：规则事实不可调；有出处的方法写明来源；"
           "经验阈值是项目自己的判断，尚未经样本检验，可在审计设置的“判断口径”里调整，报告保存当次使用的值。", ""]
    for kind in ("fact", "method", "threshold"):
        rows = [r for r in RULES.values() if r.kind == kind]
        out += [f"## {KIND_NAMES[kind]}", ""]
        if kind == "threshold":
            out += ["| 编号 | 含义 | 默认值 | 允许范围 | 适用资产 | 来源 |", "| --- | --- | --- | --- | --- | --- |"]
            for r in rows:
                low, high = r.range
                assets = "、".join(ASSET_NAMES[a] for a in r.applies_to)
                out.append(f"| {r.id} | {r.text} | {_fmt(r, r.value)} | {_fmt(r, low)} – {_fmt(r, high)} | {assets} | {r.source} |")
        else:
            out += ["| 编号 | 内容 | 来源 |", "| --- | --- | --- |"]
            out += [f"| {r.id} | {r.text} | {r.source} |" for r in rows]
        out.append("")
    out += ["## 证据等级", "", "| 等级 | 含义 |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in EVIDENCE.items()]
    return "\n".join(out) + "\n"
