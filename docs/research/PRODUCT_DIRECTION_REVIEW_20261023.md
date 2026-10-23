# 产品方向校正：竞品、文献与本地能力核验

日期：2026-10-23。结论：优先完成普通用户的“数据 → 训练 → 历史检验 → 每日策略 → 反馈与迭代”，B0 为默认路线，B1 为第二模型，B2 保留实验地位并暂停扩展。实施要求见 [产品 SPEC](../specs/PRODUCT_WORKFLOW_SPEC.md)。

## 1. 核验方法

使用 `nature-academic-search` 的多源检索和引文核验流程：Crossref 元数据/摘要、arXiv、官方项目文档与选定源码。按 DOI 合并期刊与预印本版本。这是面向实施的定向调研，不是系统综述，也没有复现论文全部实验。

本轮重新检索 Gu/Kelly/Xiu、Leippold/Wang/Zhou、经济约束、回测过拟合、SPO、TabPFN，核查 Qlib 的交易所、在线模块和 LightGBM 配置，以及 LEAN、VeighNa 官方 README。前一轮较新研究包括 MASTER、DoubleAdapt、BPQP、Kronos、Chronos-2、FinTSB 等，核验层次与限制保留在 [前期调研](E2E_RESEARCH_REVIEW_20261022.md)，不把最新论文等同于默认产品配方。

`raw.githubusercontent.com` 多次连接重置，已改用 GitHub contents/blob API 获取关键源码；Qlib arXiv HTML 已获得。一次 Qlib 题名检索无结果不用于否定该论文存在。PBO 的初次候选 DOI 返回 404，后续按题名找到并核验正确 DOI。原始下载在被忽略的 `artifacts/direction-research-20261023/`，公开仓库保留下列稳定入口。

## 2. 需要修正的评价

| 原说法 | 本轮核验与影响 |
| --- | --- |
| 产品端到端必须做 B2 | 产品流程完整与单一可微模型是不同要求；B0/B1 加组合和账户也能交付每日策略 |
| 现有每日清单已经能指导实际账户买卖 | `src/qwb/picks.py` 用同一最终模型在前后日期的入选集合判断动作，未读实际持仓、现金与可卖数量；只能算参考清单 |
| Bench 已统一提供原始股数、整手、最低佣金与退市清算 | `bench/economics.py` 的信号账户明确是分数复权单位近似，并排除多项真实执行约束；目标/生命周期账户是另一条路径，不能混称全部产品能力 |
| 尚无版本配对比较 | `bench/runner.py:compare_models` 和实验网页已有；应补 MDE、种子波动及易理解结论，避免重造 |
| Qlib 不处理涨跌停、停牌与最低佣金 | 官方 `exchange.py` 已有这些机制，支持配置成交价、数量单位和成交量限制；具体市场覆盖与数据质量仍需逐项验证 |
| 收盘成交和固定费用是 Qlib 不可改变的限制 | 它们是已检查 Alpha158 示例的设置；不能把示例默认值当作框架能力上限 |
| 模型都是 LightGBM，所以精度已经追平 | 特征、标签、处理器、股票池和数据划分不同就不能据此比较；需要共同协议实测 |
| 几个月前向模拟就能证明盈利，且成熟平台没有 | 在线更新和模拟/实盘已有成熟实现；前向运行能提供新证据并暴露运营问题，短期不证明稳定 alpha |
| 24pp/年的 MDE 说明所有模型版本都测不出提升 | 这是特定历史差值的估计；每组配对差的波动不同，必须重新估计，不能据此宣判 B2 没有验证可能 |
| CPU 精简安装还没做，任何电脑都能跑主线 | 主安装已不强制 torch/CUDA；LightGBM 可用 CPU，但最低内存、全市场耗时和 GPU 加速尚无足够跨机器证据 |

成本模型的系数显著不等于实际成交成本测准。预测 IC 很高不等于已达到行业收益水平；合成数据和数百项软件测试分别证明其覆盖范围内的软件行为，不能证明金融有效性。

## 3. 成熟项目支持与竞争边界

| 项目与已检查版本 | 看到的直接依据 | 对本项目的选择 |
| --- | --- | --- |
| [Qlib](https://github.com/microsoft/qlib/tree/54355232463878d2eebb91fe0ee5fa7fa1f5976c) | [交易所源码](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/backtest/exchange.py) 有停牌、涨跌停、费用和单位处理；[工作流](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/component/workflow.rst) 覆盖训练/记录/分析；[在线模块](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/component/online.rst) 支持新数据预测，并说明其下一日订单生成限制 | 复用配方和工作流经验，做公平参照；不声称我们已整体胜过它 |
| [Qlib LightGBM/Alpha158 示例](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml) | CSI300、TopkDropout、收盘成交、开/平仓比例费和最低费用；固定历史划分 | 原样复现与部署适配分开，不能混用公开榜单与全 A 新任务指标 |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean/blob/f3d841e60dfd010ee5fc293ea893e56952484108/readme.md) | 官方 README 明确事件驱动、回测与实盘、CLI 工作流；本轮未逐项审计其 A 股执行实现 | 借鉴策略/账户/执行分层和一致运行方式；不能说每日部署是全新发明 |
| [VeighNa/vn.py](https://github.com/vnpy/vnpy/blob/c6e231caf32b7fc97e6459817fff66458cf7e7c4/README.md) | 官方 README 的 `vnpy.alpha` 覆盖特征、Lasso/LightGBM/MLP、信号和回测；列出组合交易、数据管理和本地 `paper_account` 模块；本轮未运行这些模块 | 这是直接相关竞争者，应研究其已有 A 股/ML 工作流及未来可选成交适配，避免重复搭基础设施 |

以上以官方代码/文档证明能力存在，不证明各项目的所有市场/组合情形正确。后续引入代码或依赖前固定版本并检查 LICENSE/NOTICE、模型和数据授权，本轮没有引入这些项目的新依赖。

候选差异化是：普通用户较低门槛、自有数据与算力、可持续比较版本、清楚展示真实交易约束和证据不足。它们目前是待验收目标。不能用“更可信”“更低门槛”“已追平精度”替代公平实验与用户测试。

## 4. 文献究竟支持什么

| 文献 | 本轮核验深度 | 可吸收的依据与限制 |
| --- | --- | --- |
| Gu, Kelly & Xiu (2020), *Empirical Asset Pricing via Machine Learning*, RFS 33(5), 2223–2273. [DOI](https://doi.org/10.1093/rfs/hhaa009) | 出版元数据、摘要 | 树与神经网络等非线性方法具有经验研究基础，支持 B0 作为严肃基准；不是本项目配方有效或扣费收益的证明 |
| Leippold, Wang & Zhou (2022), *Machine learning in the Chinese stock market*, JFE 145(2), 64–82. [DOI](https://doi.org/10.1016/j.jfineco.2021.08.017) | 出版元数据；同文 [SSRN 摘要](https://doi.org/10.2139/ssrn.3754339) | A 股 ML 有直接研究基础；作者讨论流动性和短期小盘/散户效应，也报告较长周期大盘与国企预测性，不能缩写成“只在微盘有效”或推广至所有用户 |
| Avramov, Cheng & Metzker (2023), *Machine Learning vs. Economic Restrictions*, Management Science 69(5), 2587–2619. [DOI](https://doi.org/10.1287/mnsc.2022.4449) | 出版元数据、摘要 | 经济约束、难套利股票与成本会削弱回测收益；需要可交易股票池、成本敏感性和风格对照，也不能解释成所有 ML 收益必然消失 |
| Jiang, Kelly & Xiu (2023), *(Re-)Imag(in)ing Price Trends*, JF 78(6), 3193–3249. [DOI](https://doi.org/10.1111/jofi.13268) | 出版元数据、摘要 | 支持从价格观测学习表示的研究方向；图像模型的实验不证明本项目数值窗口 GRU 优于 LightGBM |
| Elmachtoub & Grigas (2022), *Smart “Predict, then Optimize”*, Management Science 68(1), 9–26. [DOI](https://doi.org/10.1287/mnsc.2020.3922) | 出版元数据、摘要 | 决策导向训练有理论与实验基础；SPO+ 的结论有适用假设，不直接证明带离散成交/路径成本的 B2 更好 |
| Bailey et al., *The probability of backtest overfitting*. [期刊 DOI](https://doi.org/10.21314/jcf.2016.322) · [预印本](https://doi.org/10.2139/ssrn.2326253) | 期刊元数据、预印本摘要 | 支持记录搜索过程、警惕反复查看测试与选择偏差；不能仅加一个置信区间就声称排除过拟合；本轮未实现其 CSCV/PBO |
| Hollmann et al. (2025), *Accurate predictions on small data with a tabular foundation model*, Nature 637, 319–326. [DOI](https://doi.org/10.1038/s41586-024-08328-6) | 出版元数据、摘要 | 表格学习仍有新进展，GBDT 并非理论终点；该文至多 10,000 样本等实验边界不能直接外推全 A 大面板、非平稳时序和净收益，不作为本轮替换 B0 的依据 |

[Qlib 论文](https://arxiv.org/abs/2009.11189) 作为平台结构阅读入口，本轮获得 HTML；对具体功能的结论以上述固定提交源码与文档为准。

## 5. 资源应该投到哪里

首先修正数据、任务、推理和账户一致性：收益预测目标与实际可执行持仓相符，更新后的特征与训练一致，费用与状态进入同一条策略闭环。这些问题会直接影响模型产出是否能使用。

随后研究模型增益：在固定协议上检验标签、流动性范围、特征、正则化、滚动训练和免交易带，再决定 GRU 或更复杂模型是否值得。预算和候选提前登记，稳定性、IC、成本、风险及净结果分别报告，保留无改善结果。

研究文献证明这些方法值得检验；成熟项目证明整套工具可实现。我们自己的真实数据实验、独立复核、外部用户成功操作和持续运行，才能证明本项目的可用性与相对表现。没有一篇论文能够替本项目保证赚钱。
