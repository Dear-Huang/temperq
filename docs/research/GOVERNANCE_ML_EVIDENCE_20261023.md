# 治理层 ML：文献依据与最小实施建议

检索日期：2026-10-23。对应 [治理层草案](../specs/GOVERNANCE_SPEC.md)，不冻结或替换该草案，不代表功能已实现。本轮核对了 Crossref 元数据、部分作者/出版社摘要、官方项目文档和许可证；未逐篇复现论文，未完成系统综述或实盘审计。正式发表版本与预印本按题名/作者去重，优先引用正式版本。不把论文作者的业绩数字当作 TemperQ 可达到的目标。

## 1. 结论

有成熟的理论和工程，支持“用户定授权，ML 估计风险或机会，决策器在约束内形成目标持仓”。它通常叫 portfolio construction、dynamic asset allocation、risk forecasting、decision-focused learning，不一定使用“治理层”这个名称。

建议的产品方向是 **用户设计授权和决策方式，模型帮助执行，平台记录并检验结果**。训练和外部模型导入是两个信号入口，随后共用治理、成交和前向跟踪。不要把产品缩成审计器，也不要靠“必须每个人训练不同的预测器”证明个性化。

治理可以包含 ML，ML 也可以最终直接学习权重；没有“B2 天生不能成为核心”的理论结论。当前 B2 不宜作为默认产品能力，是因为本项目尚无足够的真实数据验证、费用一致性和用户流程证据，而不是该领域没有研究。

## 2. 文献与适用范围

| 环节 | 核对到的来源 | 支持什么 | 不支持什么 / 本项目采用建议 |
| --- | --- | --- | --- |
| 信号来了要不要立即满仓 | Gârleanu & Pedersen (2013), *Dynamic Trading with Predictable Returns and Transaction Costs*, Journal of Finance 68(6), 2309–2340. [DOI](https://doi.org/10.1111/jofi.12080), [作者摘要/NBER](https://www.nber.org/papers/w15205) | 有成本且信号衰减速度不同的动态交易理论；部分向目标调整，考虑未来目标变化 | 论文实证是商品期货，不直接证明 A 股排名免交易带有效；原文有 [2021 corrigendum](https://doi.org/10.37214/jofweb.4)，实现公式前需核对。支持“节奏和成本值得单独设计” |
| 风险预测后调整仓位 | Christensen, Siggaard & Veliyev, *A Machine Learning Approach to Volatility Forecasting*, Journal of Financial Econometrics 21(5), 1680–1727（2022 在线、2023 卷期）. [DOI](https://doi.org/10.1093/jjfinec/nbac020) | 比较正则回归、树、神经网络与 HAR；样本中 ML 可改进 realized variance 预测 | 研究使用美股 realized variance，不能把仅有日线的数据说成复现；日线可做自己的低频风险代理实验。另有 [correction](https://doi.org/10.1093/jjfinec/nbac032)，采用前核对 |
| 波动控制的经济用途 | Moreira & Muir (2017), *Volatility-Managed Portfolios*, Journal of Finance 72(4), 1611–1644. [DOI](https://doi.org/10.1111/jofi.12513), [NBER](https://www.nber.org/papers/w22208) | 高波动时减少风险敞口的研究依据 | 波动目标本身是控制规则，不是 ML；不能写“几乎不会过拟合”或保证提升收益 |
| 波动控制的反证和边界 | Cederburg, O'Doherty, Wang & Yan (2020), *On the performance of volatility-managed portfolios*, Journal of Financial Economics 138(1), 95–117. [DOI](https://doi.org/10.1016/j.jfineco.2020.04.015) | 需要检查可实现的样本外表现，不能只依赖原始研究样本 | 本轮核对到发表元数据，未取得全文；不得据此填写精确收益/显著性数字。实现前需读全文并复核其样本外结论 |
| 用预测、风险、成本共同决定仓位 | Boyd et al. (2017), *Multi-Period Trading via Convex Optimization*, Foundations and Trends in Optimization 3(1), 1–76. [DOI](https://doi.org/10.1561/2400000023), [工程](https://www.cvxportfolio.com/) | 单期/多期目标函数整合预期收益、风险、交易成本和持有成本；支持 MPC，即规划多期、先执行第一期 | 优化器不是 ML，也不创造 alpha；A 股整手、T+1、最低佣金、涨跌停不能假装是连续凸模型已精确解决 |
| 直接训练仓位政策 | Brandt, Santa-Clara & Valkanov (2009), *Parametric Portfolio Policies: Exploiting Characteristics in the Cross-Section of Equity Returns*, Review of Financial Studies 22(9), 3411–3447. [DOI](https://doi.org/10.1093/rfs/hhp003), [NBER](https://www.nber.org/papers/w10996) | 直接把资产特征映射为权重，以投资者效用估计少量政策参数；不是必须用深网或 RL | 权重政策学习已有长期文献；不等于个人 A 股账户上已获验证。可作为后续低维可学习治理的理论起点 |
| 判断已有信号是否值得执行 | López de Prado (2018), *Advances in Financial Machine Learning*, Wiley, ISBN 9781119482086，§3.6–3.7 Meta-Labeling、Chapter 10 Bet Sizing. [出版社及目录](https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086) | 主模型负责方向，第二模型估计是否执行及规模的建模方法 | 方法有出版依据，但本轮未找到足以证明“元标签普遍增加 A 股净收益”的强证据；列为待验证候选，不能标成收益已成熟 |
| 神经网络直接学权重 | Zhang, Zohren & Roberts (2020), *Deep Learning for Portfolio Optimization*, Journal of Financial Data Science 2(4), 8–20. [DOI](https://doi.org/10.3905/jfds.2020.1.042), [作者预印本](https://arxiv.org/abs/2005.13665) | 直接优化组合 Sharpe，研究成本敏感性和波动缩放 | 论文用跨资产指数 ETF，不能外推到 A 股全市场及最低佣金账户；支持 B2 路线存在，不支持本项目 B2 已可上线 |
| 预测与组合联合训练 | Butler & Kwon (2023), *Integrating prediction in mean-variance portfolio optimization*, Quantitative Finance 23(3), 429–452. [DOI](https://doi.org/10.1080/14697688.2022.2162432)；Uysal, Li & Mulvey, *End-to-end risk budgeting portfolio optimization with neural networks*, Annals of Operations Research 339, 397–426（2023 在线）. [DOI](https://doi.org/10.1007/s10479-023-05539-4) | 可微优化层、直接学习风险预算已有正式发表研究 | 工程研究路线可行，不是已证明持续实盘胜过分离训练。成本代理、整数执行和未知市场分布仍要本地验证 |

“预测 ML → 组合优化”与“端到端政策学习”都能参与治理。前者更容易分模块检验和解释；后者可作为比较候选保留，不能用术语决定优劣。

## 3. 能复用哪些工程

本次读取的是当日默认分支及在线文档，不是代码安全审计或生产认证。实际接入时固定版本/commit，复核依赖和许可；本轮未将任何库加入运行环境。

| 工程 | 已核对的能力与许可证 | TemperQ 的用途与边界 |
| --- | --- | --- |
| [cvxportfolio](https://www.cvxportfolio.com/en/stable/) / [源码](https://github.com/cvxgrp/cvxportfolio) | 单期/多期策略、收益/风险/成本模型；LongOnly、MaxWeights、ParticipationRateLimit、TurnoverLimit 等约束；当前源码 GPL-3.0，不要沿用早期 Apache 的说法。官网说明自 2023 年底每日提交示例目标权重和初始持仓 | 优先做目标持仓生成器的参考和交叉核验；保留 TemperQ 现有 A 股账户成交引擎。其公开日更是工程持续运行证据，不等于盈利认证 |
| [skfolio](https://skfolio.org/) / [源码与许可](https://github.com/skfolio/skfolio) | sklearn 风格的组合优化、风险模型、模型选择和验证工具；BSD-3-Clause | 适合构建“风险估计器 + 配置化组合器”的训练/比较流程。其通用回测不能直接替代 A 股逐股成交与现金账本 |
| [Qlib](https://github.com/microsoft/qlib) | MIT；覆盖风险模型、组合优化、执行、RL，不能说它只做预测或不提供治理 | 作为外部信号适配、策略接口和公开对照的来源；不另建一个完整 Qlib 克隆。具体 RL 文档链接本轮有 404，不视为模块不可用 |
| [CVXPYlayers](https://github.com/cvxgrp/cvxpylayers) | Apache-2.0，可微凸优化层 | B2/联合训练的研究工具；不是完整交易引擎，不能负责最低佣金和整手的精确会计 |
| [deepdow](https://github.com/jankrepl/deepdow) | Apache-2.0，深度学习和组合层组件；README 明确频繁短期交易成本不是其主要关注点 | 参考层设计和实验配方；不直接当 A 股成本感知治理器，也未核定其近期维护质量 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 原 FinRL MIT，README 定位教育/研究，另推 FinRL-Trading；后者本轮 LICENSE 为 Apache-2.0 | RL 研究对照，不能把项目自称 production-oriented 当成 A 股实盘可靠性的独立证明 |
| [mlfinlab](https://github.com/hudson-and-thames/mlfinlab) | 当前 README 明示 all rights reserved | 不直接复制其元标签代码；可按书中方法独立实现 sklearn/LightGBM 小模型，另核查任何候选开源实现的具体版本许可 |

最小工程选择：复用已有 LightGBM、账本和时间切分；以 cvxportfolio/skfolio 作对照和接口参考。先做依赖、CPU 资源和目标一致性探针，再决定是否接入库，不引入第二套撮合器。

## 4. 第一项 ML 应该训练什么

建议先试 **风险预测头**，与纯规则治理共用完全相同的信号、授权书、成本和执行，不先训练一个包办触发/仓位/止损的黑箱。

```text
信号分数 + 截至决策时刻的行情 + 当前账户状态
        ↓
风险预测头：估计未来 5 日的风险量（每日数据只能定义低频代理）
        ↓
确定性控制：波动目标、敞口上下限、单票上限、换手约束
        ↓
目标权重 → 整股/现金/可卖校验 → 次日成交模拟 → 决策及受阻记录
```

用户可以选择“固定仓位”或“风险预算”，指定目标风险、最大仓位、换手预算，并查看模型估计如何影响当日建议。风险模型不能改用户的授权或成交规则。界面展示未约束目标、约束后的目标、实际成交和原因，用户能参与且不必调神经网络超参数。

风险预测的最低试验：固定未来 5 日标签和一个可解释的小型树/线性模型；与 EWMA、滚动波动基线比较。标签使用当时可定义的资产/持仓集合，不以未来选出的股票回填历史篮子。日线平方收益不是分钟数据的 realized variance，应单独命名。除预测损失（如正值方差预测的 QLIKE）外，检查实际敞口、费用、风险跟踪误差、换手与净收益；“预测好”不直接推出“治理有用”。

下一个候选是 **执行价值/元标签头**：用滚动样本外信号训练，不使用基础模型在自身训练集上产生的分数；按日期分组，清除标签跨边界的样本。不能把同一天几千只股票当成几千个独立市场样本。标签要区分“该股票未来涨没涨”和“执行这次交易相对继续持有/现金是否有增量价值”；后者还依赖卖出什么、交易费用和机会成本。仅预测胜率大于 50% 也不等于期望净收益为正。

Ridge/LightGBM 风险头和小型元标签头可以设计成 CPU 方案；全市场十年、重复滚动和治理回放的具体耗时/内存必须实测，不沿用“必定一分钟内”等估计。决策聚合器/凸优化器也不天然需要 GPU；B2 和 RL 是否需要 GPU 取决于规模与算法，不能按名称一概断言。

## 5. 对当前草案的必要修正建议

1. “预测相关度 0.95–0.97，所以竞赛只比谁数据多”不是普遍结论。若来自特定实验，应附样本、模型和度量；数据更多不保证更好，相关度高也不等于组合或净收益相同。
2. 固定历史回放的费用/换手可以精确重算，但未来成本仍有模型误差；最大回撤是路径极值，通常很不稳定，不能说“回撤可以测准”。
3. `max_drawdown` 只能是降风险触发阈值，不能保证实际最大亏损不超过它。跳空、跌停、T+1 和停牌会阻止执行；“目标超限”与“实际持仓超限且无法卖出”须分开呈现。
4. 波动目标是控制，网格选 K 是选参；真正的 ML 估计器另列。元标签先列实验假设，不写默认有效。第一项 ML 优先增加可检验的风险头，而不是承诺所有环节一起学。
5. 基础模型分数通常是排名，不能不经训练期校准就当成预期收益与 bp 成本相减；所需校准参数、信号模型版本、数据快照、决策时刻和延迟必须进入缓存身份，仅 `(来源, 日期)` 不够。
6. “六项规则 + 授权书 + 全市场 8 年 15 秒 + 止损 + 大盘开关”不是一个小交付。第一步只做持仓数、单票上限、排名免交易带及固定 5 日节奏；止损、大盘择时和 ML 风险头分别交付。首版采用明确运行按钮，避免滑块每次移动都隐式增加一次回测尝试。
7. 对照时固定同一信号和初始账户；仓位不同时补充相同风险/平均敞口的对照，避免把长期多拿现金误认成 ML alpha。历史样本外比较、成本压力和前向观察各司其职，不把所有历史证据一概抹掉，也不把几个月前向观察当成充分的盈利证据。
8. 本地哈希/尝试台账可以校验一致性，不能证明未删除历史或未在别处试验；不宣称“不可篡改审计证书”。最大回撤排序、私下百分位也都是绩效比较，不因换名称而自动消除选择偏差。

这些是供产品负责人修改草案的建议，本轮不直接改写待审 SPEC，也不改变现有冻结协议/账户。

## 6. 最小实施顺序与停止线

| 交付 | 只做什么 | 完成即停 |
| --- | --- | --- |
| G0 共用信号/账户契约 | 固定一个现有模型/预测集、初始账户、5 日节奏、成本声明和身份 | 能解释一份信号如何变成当前目标；官方/自训/外部三类入口先选一类验收，其他为适配器 |
| G1 可解释规则对照 | 持仓数、单票上限、免交易带三项；相同输入跑两组配置；逐笔原因 | 与旧默认配置一致；现金、整股、T+1 一致；一位用户能说出一次规则改变为何改变成交/费用。先不做社区 |
| G2 成本口径和风险基线 | 不扩展优化自由度；接入一致的价差成本或明确做费用压力，保留原始净/毛分项；EWMA 对照 | 不把佣金/印花税账本称为全成本；能复算费用、持仓和风险跟踪误差 |
| G3 第一个可学习治理组件 | 固定一个风险头，和相同授权的 EWMA 控制比较 | 无标签跨界/基础模型泄漏；报告预测质量及经济影响。没增益也完成交付，保留纯规则默认值 |
| G4 前向双账户 | 冻结纯规则和 ML 治理两套策略，真实日更并排记录 | 自动运行、失败/漏记可见、修改生新版本。日历证据持续积累，不用加班“补足” |

现有本地训练保留，外部体检是同一评价能力的另一个入口。运营上先演示“同一信号、两份授权、不同决策的原因与后果”，再邀请用户分享授权/规则卡；不以因子库数量、模型独特性或收益排行榜作为首轮成功标准。B1 和 B2 按各自真实数据证据推进，不因治理方向而删掉，也不一起排进这一轮验收。

## 检索限制

Crossref 核对到了上表文献；arXiv/语义搜索若干精确查询无结果，因此直接核对作者预印本及出版社。Cederburg 全文未取得，ScienceDirect 一个尝试返回 403，不虚构全文结论；Qlib 部分深层链接 404，改看官方 README；GitHub README 中项目自报的生产可用、回测数字均未当作独立证据。未核验此前对话中提到的 alphaverdict、monte-neo、AlphaAudit 等名称及数字，本说明不以它们支持方向判断。
