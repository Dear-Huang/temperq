"""Parameter rule book: what the literature and this project's own tests say
about each setting, applied to one concrete run config.

Each item: level ("locked" = enforced by the code, "warn" = a setting that
weakens the result, "ok" = checked and fine), the rule, why, and the source.
Nothing here blocks a run: settings stay open, deviations stay visible.
"""

from __future__ import annotations

from . import daily, scenario

SRC = {
    "lopez": "López de Prado (2018)《Advances in Financial Machine Learning》第 7 章：清除与隔离（purging / embargo）",
    "pbo": "Bailey, Borwein, López de Prado & Zhu (2017)，回测过拟合概率 PBO，Journal of Computational Finance",
    "hlz": "Harvey, Liu & Zhu (2016)，“…and the Cross-Section of Expected Returns”，RFS：新因子 t 值门槛约 3",
    "law": "Grinold & Kahn《Active Portfolio Management》：主动管理基本定律 IR ≈ IC × √宽度",
    "ac": "Almgren & Chriss (2000)，最优执行与冲击成本，Journal of Risk",
    "lgb": "LightGBM 官方文档《Parameters Tuning》：叶子数、最小叶子样本数与过拟合",
    "seed": "本项目实测（ML 同质化研究）：换一个随机种子，预测相关 0.945，持仓却换掉约 31%",
    "tick": "本项目实测（When the Tick Binds）：A 股价差受 1 分钱最小报价单位约束，固定费率低估低价股成本",
    "real": "本项目实测：收盘价成交不可实现；按次日开盘成交、涨停买不进、跌停卖不出",
    "cost": "本项目实测：全 A 股 5 日反转信号毛超额 +0.9%/年，扣成本后 −9.6%/年",
}

UNIVERSE_SIZE = {"csi300": 300, "csi500": 500, "csi1000": 1000, "st": 150}


def _item(level, rule, why, src, fix=""):
    return {"level": level, "rule": rule, "why": why, "source": SRC.get(src, src), "fix": fix}


def check(cfg: dict, card: dict, years_of_data: int | None = None) -> list[dict]:
    out = []
    h = int(card["label"]["end"]["day"]) - int(card["label"]["start"]["day"])
    model = cfg.get("model", "lightgbm")
    pf = cfg.get("portfolio", {})
    n_hold = pf.get("n_hold")
    u = cfg.get("universe", "all")

    # --- enforced by the code -------------------------------------------------------
    out.append(_item("locked", "逐折按标签成熟时间过滤训练样本",
                     "训练只使用拟合截止时点已知的标签，具体清除日期与样本见 folds.json。", "lopez"))
    out.append(_item("locked", "内置训练与选轮数不使用当前折测试标签；真实数据测试查看写入本地台账",
                     "台账不能阻止外部调参或删除记录；反复查看后仍属于开发证据。", "pbo"))
    out.append(_item("warn", "训练报告的简化费用诊断不能替代独立账户",
                     "完整现金、持仓保留、逐笔拒单和状态缺口以 QW-Bench 为准；两种口径不能混用。", "real",
                     "在同一冻结协议下运行 Bench，检查账户资格与费用压力"))
    out.append(_item("locked", "默认费用诊断使用个股 STRUCT 价差估计",
                     "它仍是模型估计，不包含真实冲击、队列或容量；固定费率对照仅用于口径敏感性。", "tick"))
    if u in UNIVERSE_SIZE and u != "st":
        out.append(_item("locked", "指数成分按每个历史时点当时的名单",
                         "用今天的成分股回看过去，会漏掉后来被调出的股票（幸存者偏差）。", "lopez"))

    # --- settings worth a warning ---------------------------------------------------
    if h == 1:
        out.append(_item("warn", "持有 1 天：换手最高，成本最容易吃掉收益",
                         "每天换仓，成本按天累计；排序能力强不等于扣成本后赚钱。", "cost",
                         "先用持有 5 天对照，看扣成本后的差别"))
    if h >= 20:
        n_ind = int(2 * 244 / h)
        out.append(_item("warn", f"持有 {h} 天：两年测试期只有约 {n_ind} 个互不重叠的持有期",
                         "独立样本太少，t 值不稳定，容易把运气当成能力。新因子的 t 值门槛建议约 3。", "hlz",
                         "看报告里的 t 值和 MDE；t < 3 时不要当成确定的结论"))
    if n_hold is not None and n_hold < 20:
        out.append(_item("warn", f"只持有 {n_hold} 只股票",
                         "持仓太少时，实际收益主要由个股的偶然波动决定，模型排序能力很难兑现。", "law",
                         "资金允许的话持有 20 只以上"))
    size = UNIVERSE_SIZE.get(u)
    if u == "list":
        size = len(cfg.get("universe_list") or [])
    if size and n_hold and n_hold > 0.3 * size:
        out.append(_item("warn", f"持有 {n_hold} 只，占股票池（约 {size} 只）的 {n_hold / size:.0%}",
                         "几乎把股票池买全了，模型挑不挑股票差别不大。", "law",
                         "减少持股只数，或换更大的股票池"))
    if u == "st":
        out.append(_item("warn", "只做 ST 股：截面只有一两百只",
                         "每天可比较的股票少，IC 噪声大，需要更长的测试期才能下结论。", "law"))
    if model == "lightgbm":
        seeds = cfg.get("seeds", daily.DEFAULTS["seeds"])
        if len(seeds) < 3:
            out.append(_item("warn", f"只训练 {len(seeds)} 次（种子）",
                             "看不到随机种子带来的差异有多大，没法判断模型之间的差别是不是噪声。", "seed",
                             "至少 3 次"))
        if cfg.get("selection"):
            out.append(_item("locked", "使用清除重叠标签的历史尾部验证选择轮数，再在全部成熟历史上重拟合",
                             "验证窗长度与早停耐心也属于模型选择，应预先固定；单个验证窗仍可能过拟合。", "lgb"))
        else:
            out.append(_item("warn", "LightGBM 使用固定训练轮数",
                             "固定轮数适合受控对照；不要通过反复查看测试期来挑轮数。", "lgb",
                             "可在 YAML 使用 model.selection，或在外部研究环境固定训练方案后提交预测"))
        lg = cfg.get("lgbm", {})
        if lg.get("num_leaves", 63) > 255 or lg.get("min_data_in_leaf", 1000) < 100:
            out.append(_item("warn", "树太复杂（叶子数多或每片叶子样本少）",
                             "金融数据信噪比低，复杂的树容易记住历史噪声。", "lgb",
                             "叶子数 ≤ 255，每片叶子样本 ≥ 数百"))
        if lg.get("learning_rate", 0.05) > 0.1:
            out.append(_item("warn", "学习率偏大",
                             "学习率大时少数几棵树就拟合了噪声，结果对参数很敏感。", "lgb", "0.01–0.1"))
    capital = (cfg.get("min_adv_yuan") or 0) * scenario.PARTICIPATION * (n_hold or 0)
    if capital >= 1e8:
        out.append(_item("warn", f"资金量约 {scenario.fmt_yuan(capital)}：没有计入大单冲击成本",
                         "仓位占成交额比例越大，冲击成本越高；报告里的成本是小单成本，会低估。", "ac",
                         "把净超额当作上限看待"))
    comm = pf.get("commission_bp", daily.DEFAULTS["portfolio"]["commission_bp"])
    if comm < 1:
        out.append(_item("warn", f"佣金 {comm:g}bp 低于常见水平",
                         "A 股佣金一般在万一到万三（1–3bp）单边，过低会高估净收益。", "cost"))
    if years_of_data is not None and years_of_data < 5:
        out.append(_item("warn", f"数据只有 {years_of_data} 年，最终测试期只剩 1 年",
                         "测试期越短，能检出的最小效应越大，结论越不确定。", "hlz",
                         "准备 5 年以上数据"))
    if not any(i["level"] == "warn" for i in out):
        out.append(_item("ok", "没有发现会削弱结论的参数设置", "", ""))
    return out


def as_markdown(items: list[dict]) -> list[str]:
    mark = {"locked": "已锁定", "warn": "注意", "ok": "通过"}
    lines = ["## 参数体检（依据文献与本项目实测）", ""]
    for i in items:
        lines.append(f"- **{mark[i['level']]}**：{i['rule']}。{i['why']}"
                     + (f"建议：{i['fix']}。" if i["fix"] else "")
                     + (f"（{i['source']}）" if i["source"] else ""))
    return lines + [""]
