# 端到端量化研究：文献、项目复用与技术决策

检索与核验日期：2026-10-22。用途：为下一阶段研发选型提供依据。本文没有宣称在本项目复现了所列论文。

实施依据见 [端到端研究平台 SPEC](../specs/E2E_RESEARCH_PLATFORM_SPEC.md)。现有能力见 [研究闭环](../RESEARCH_WORKFLOW.md)，前期平台对标见 [专业研究链路审查](../PROFESSIONAL_RESEARCH_AUDIT_20261022.md)。

## 1. 结论与取舍

“原始行情到预测”和“原始行情到持仓、按净效用训练”都有成熟研究基础，值得做。第一阶段选择可检查的小型序列编码器、显式风险与成本组合层、独立账户回放；先验证各环节确实一致，再扩大模型。因子作为可选输入和对照，不作为端到端模型的强制中间产物。

不从零写凸优化器，也不以强化学习、大规模预训练或公告多模态作为首个交付。Qlib 的模型与数据处理模式、CVXPYlayers 的求解与反传、现有 Bench 的独立评价职责可以互补。复用组件仍须验证版本、许可、数据时点和账户语义，不能将上游默认配置直接称为本平台已通过验证的能力。

平台竞争已经覆盖“全链路”“多模态”“金融基础模型”。可检验的差异应落在：A 股时点数据、实际持仓与受限成交、训练近似与独立账户的一致性、完整试验台账、专业用户可替换接口，以及别人能独立运行的公开基准。这些是研发目标，尚不是已证明的竞争优势。发布关注度需要外部复现和真实用户检验。

## 2. 检索方法与证据边界

- 来源：Crossref 出版元数据与摘要、arXiv 元数据及部分全文、官方 GitHub README 与选定源码；使用 `nature-academic-search` 工作流组织检索、去重与核验。
- 查询覆盖：direct portfolio optimization、decision-focused learning、differentiable convex optimization、BPQP、原始价格表示、Kronos、Chronos-2、FinTSB、FinWorld、MASTER、DoubleAdapt。
- 这是面向工程选型的定向调研，不是系统综述。宽泛查询出现无关书章，Semantic Scholar 查询返回空结果，均未用作“没有相关工作”的证据。
- DOI 去重，期刊/会议版本优先于同文 SSRN/arXiv；预印本保留为全文入口。最新日期不等于证据更强。
- “全文”表示获取并检查了正文/相关方法与实验边界；“摘要”表示未核验全文细节。作者报告的收益和榜单不等于本项目实测结果。代码存在、近期更新、GitHub 星数均不能证明投资有效性。
- 下载全文与 README 原文留在被忽略的本地 `artifacts/e2e-research-20261022/`，不随仓库转发论文、数据或权重。下文保留公开链接和代码 blob 身份，便于重新核验。

## 3. 关键文献

| ID | 文献与稳定入口 | 已核验内容 | 对本项目的用途与限制 |
| --- | --- | --- | --- |
| P01 | Zhang, Zohren & Roberts. **Deep Learning for Portfolio Optimization**. JFDS 2(4), 2020. [DOI](https://doi.org/10.3905/jfds.2020.1.042) · [全文预印本](https://arxiv.org/abs/2005.13665) | 全文、出版元数据；小型 LSTM 直接训练组合 Sharpe，含成本敏感性 | 直接配置不必先引入 RL。实验仅四个美国指数相关序列，不能推出高维 A 股可交易性；资产/指数与可交易工具须另核对 |
| P02 | Jiang, Kelly & Xiu. **(Re-)Imag(in)ing Price Trends**. Journal of Finance 78(6), 2023. [DOI](https://doi.org/10.1111/jofi.13268) | 元数据、摘要 | 支持从价格观测自动学习表示；不能据此认定图像优于数值窗口或能迁移至 A 股。先采用易检查的数值输入 |
| P03 | Elmachtoub & Grigas. **Smart “Predict, then Optimize”**. Management Science 68(1), 2022. [DOI](https://doi.org/10.1287/mnsc.2020.3922) | 元数据、摘要 | 下游决策误差可进入训练；SPO/SPO+ 有相应目标与假设，不能直接等同于带路径成本的真实交易账户 |
| P04 | Agrawal et al. **Differentiable Convex Optimization Layers**. NeurIPS 2019. [全文](https://arxiv.org/abs/1910.12430) | 全文；DPP、仿射-求解-仿射结构与隐式微分 | 首个可微组合后端依据。在不可微或不可逆点，论文讨论启发式最小二乘导数；求解成功不代表梯度正确 |
| P05 | Pan et al. **BPQP: A Differentiable Convex Optimization Framework for Efficient End-to-End Learning**. NeurIPS 2024. [全文](https://arxiv.org/abs/2411.19285) | 全文；后向转成简化 QP；Alpha158/CSI500 实验 | 后续性能候选。附录明确不计交易成本，不能将其组合指标作为净效用证据。Qlib 当前 README 的集成仍标注 “Coming soon! Under review”，不作为已发布依赖 |
| P06 | Shi et al. **Kronos: A Foundation Model for the Language of Financial Markets**. AAAI 40(30), 25366–25373, 2026. [DOI](https://doi.org/10.1609/aaai.v40i30.39730) · [预印本](https://arxiv.org/abs/2508.02739) | 出版元数据、摘要、官方 README | 金融 K 线预训练对照；并非本平台首个默认模型。README 明示微调示例不是生产交易系统。必须记录预训练截止、市场重叠、权重版本，不能默认历史零样本无污染 |
| P07 | Ansari et al. **Chronos-2: From Univariate to Universal Forecasting**. 2025. [arXiv](https://arxiv.org/abs/2510.15821) | 摘要、官方 README | 通用多变量/协变量预测对照；不能把通用时间序列领先换成金融 alpha 或净收益领先 |
| P08 | Berthelier et al. **Investigating simple target-covariate relationships for Chronos-2 and TabPFN-TS**. 2026. [arXiv](https://arxiv.org/abs/2605.12200) | 摘要 | 提醒检查简单受控关系和输入利用情况；其比较不是 A 股投资实验，不能据此判定金融模型胜负 |
| P09 | **A comprehensive and practical benchmark for financial time series forecasting (FinTSB)**. Frontiers of Computer Science 20(10), 2026. [DOI](https://doi.org/10.1007/s11704-026-51064-5) · [预印本](https://arxiv.org/abs/2502.18834) | 元数据、摘要、官方 README | 学习统一评价与现实约束的组织方式。预处理仍有待开放部分，数据需用户另行授权取得，不是完整开箱复现数据集 |
| P10 | Zhang et al. **FinWorld: An All-in-One Open-Source Platform for End-to-End Financial AI Research and Deployment**. 2025. [arXiv](https://arxiv.org/abs/2508.02292) | 摘要、官方 README、项目配置 | 直接相关的平台竞争者，覆盖多模态/DL/RL/LLM；README 标注 Preview，部分组件可能不可用，不应把论文覆盖等同于完整成熟实现 |
| P11 | Fernandes & Desell. **Financially Guided Deep Portfolio Optimization**. IEEE CIFEr, 114–121, 2026. [DOI](https://doi.org/10.1109/CIFEr67845.2026.11692398) · [预印本](https://arxiv.org/abs/2605.28853) | 元数据、摘要 | 风险与收益目标可微化的近期例子；50 只美股、两年样本外等作者实验范围，不能替代 A 股时点、交收和容量检验；本文未核验其全文成本实现 |
| P12 | Kim, Tae & Lee. **Estimating Covariance for Global Minimum Variance Portfolio: A Decision-Focused Learning Approach**. 2025. [arXiv](https://arxiv.org/abs/2508.10776) | 摘要 | 风险估计的决策导向训练候选；GMVP 的解析梯度不直接覆盖收益预测、动态约束、离散成交 |
| P13 | **MASTER: Market-Guided Stock Transformer for Stock Price Forecasting**. AAAI 38(1), 162–170, 2024. [DOI](https://doi.org/10.1609/aaai.v38i1.27767) | 元数据、摘要 | 后续市场状态与跨股票交互对照；它自身不是原始输入到可交易持仓的完整链路 |
| P14 | **DoubleAdapt: A Meta-learning Approach to Incremental Learning for Stock Trend Forecasting**. KDD, 3492–3503, 2023. [DOI](https://doi.org/10.1145/3580305.3599315) | 出版元数据 | 泛化滚动折之后再研究适应性训练；本轮未核验全文，不据此设默认超参数或声称增益 |
| P15 | Zhang & Zohren. **Deep Learning in Quantitative Trading**. Cambridge, 2025. [DOI](https://doi.org/10.1017/9781009707091) | 出版元数据、摘要 | 方法组织与后续阅读入口；未读取全书或验证其伴随代码，不作为本项目已复现的方法来源 |

P01 的 SSRN `10.2139/ssrn.3613600`、P02 的 SSRN `10.2139/ssrn.3756587` 不另计为独立研究。P06 的会议状态已通过 Crossref DOI 核实，依据不再仅是作者的录用公告。P11 已核实会议 DOI，仍与“已经全文复核”分开。

## 4. 开源项目复用审查

许可与最近推送为检索日 GitHub 元数据快照；后续版本可能变化。许可标识是初筛依据，正式引入须核对所选提交的 LICENSE、NOTICE、传递依赖、模型权重和数据条款。本轮不引入新依赖。

| 项目 | 许可 / 最近推送 | 建议复用 | 当前限制与处理 |
| --- | --- | --- | --- |
| [Qlib](https://github.com/microsoft/qlib) | MIT / 2026-10-22 | ALSTM/GRU、Alpha360 时序组织、适配与工作流模式 | 不搬整个平台。已读的示例是 CSI300、固定 2008–2020 划分、收盘成交与旧费率，不能直接作为本项目协议；BPQP 集成未作为已发布能力核验 |
| [CVXPYlayers](https://github.com/cvxpy/cvxpylayers) | Apache-2.0 / 2026-09-24 | 首个可微凸优化后端 | DPP、求解器、双精度梯度、Windows/Linux 兼容必须实测；当前主分支要求 Python ≥3.11、CVXPY ≥1.9，不能假定已有环境直接兼容 |
| [cvxportfolio](https://github.com/cvxgrp/cvxportfolio) | GPL-3.0 / 2026-04-27 | 风险/成本/约束建模与独立对照设计 | 不直接复制进 MIT 主干；作为可选对照前明确分发方案和许可要求，不能假定进程隔离自动消除义务 |
| [deepdow](https://github.com/jankrepl/deepdow) | Apache-2.0 / 2024-01-24 | 组合层和损失接口参考；已有 DPP Markowitz 实现 | 维护与版本兼容需评估。README 面向长持有期，频繁换仓成本不是其重点；窗口损失不等于有实际持仓状态的 A 股账户 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT / 2026-10-20 | 日后 RL 对照与环境接口参考 | 当前 README 将本仓库定位为原教育/研究框架；教程不能作为实盘就绪证明；第一阶段无需引入 RL |
| [FinWorld](https://github.com/DVampire/FinWorld) | MIT / 2025-10-07 | 多任务、模态与竞争定位参考 | Preview、组件完整性待查。暂不引入其整套依赖或宣称比其完整 |
| [Kronos](https://github.com/shiyu-coder/Kronos) | MIT / 2026-04-13 | 可选预训练编码/预测对照 | 代码许可不能代替权重、训练数据授权和污染核查；官方微调示例需要生产研究链路补全 |
| [FinTSB](https://github.com/TongjiFinLab/FinTSB) | GPL-3.0 / 2025-08-22 | 独立基准与约束对照 | 不内嵌 GPL 代码；README 说明部分预处理待开放、行情另行取得 |
| [Chronos](https://github.com/amazon-science/chronos-forecasting) | Apache-2.0 / 2026-10-05 | 通用预训练模型对照 | 权重/数据版本另核对；多变量预测能力不等于组合净效用能力 |
| [Time-Series-Library](https://github.com/thuml/Time-Series-Library) | MIT / 2026-04-18 | 选取少量基线实现 | README 说明维护资源有限、暂无主动新增功能，旧榜单可能失去意义。只选实现，不导入整套依赖或搬用榜单结论 |

### 可追踪的检查记录

README 的 Git blob 身份如下；blob 是文件内容身份，不是整个仓库提交，不能用它替代依赖版本锁。正式实施时须另固定完整提交或发行版。

| 仓库 | 已读取 README blob |
| --- | --- |
| microsoft/qlib | `a6963f6e40bd9ebee7dd2b5c88df94fe73a67dd4` |
| cvxpy/cvxpylayers | `8731ca1e60101fdafebf0e2d11d3fca1dbf2e6e6` |
| cvxgrp/cvxportfolio | `5ad5b8e4968ae76bba5c26ec81c9c90b05c31407` |
| jankrepl/deepdow | `b4bf15b695fcc37957c54bfbc49db71344c1856b` |
| AI4Finance-Foundation/FinRL | `50e026b2c8715f4683f548afde0d782da777fc9e` |
| DVampire/FinWorld | `5911e0252696bafc4c3b1b49010e6b60665111e7` |
| shiyu-coder/Kronos | `faffa1578cf0172f6b88f5db8a9b1205668ffef1` |
| TongjiFinLab/FinTSB | `50684f4edd59b26768111c7a7a34b4eab5cd7b0b` |
| amazon-science/chronos-forecasting | `1ec146a2e037a7725f53ae6bdfba22970804efb2` |
| thuml/Time-Series-Library | `612c92c4d1e25b234e7a054f6f4374949d5ade35` |

选定源码检查，不等于整体审计：

- Qlib `examples/benchmarks/ALSTM/workflow_config_alstm_Alpha360.yaml`，blob `b345cacd9814f90d13c6462167717e7d532490ef`：GRU、6 通道、RobustZScoreNorm、训练/验证/测试区间、TopkDropout、成交与费用配置；标签为未来两日/一日收盘比值，不直接沿用到新账户任务。
- Qlib `qlib/contrib/model/pytorch_alstm.py`，blob `d1c619ebf41dc7f752e4d51ae02df156266921fc`：ALSTM、MSE 与设备路径。输入组织与归一化须通过自有时点契约适配。
- deepdow `deepdow/losses.py`，blob `d16d2c0915ff48f53f49e09ac00deb28c83875f0`；`deepdow/layers/allocate.py`，blob `d4aaee07759159317aaa8ed50d4886756615eafa`：权重/收益窗口损失、解析与 DPP 数值 Markowitz。不能据此宣称具备本项目交收、未成交或退市生命周期处理。
- CVXPYlayers 旧路径 `cvxpylayers/torch/cvxpylayer.py` 返回 404；通过官方树定位到 `src/cvxpylayers/torch/cvxpylayer.py`，blob `e118a9ee8f5e1bdcea604891d7721e446ba32423`。已核对 `CvxpyLayer(problem, parameters, variables, solver, solver_args)`、DPP 要求和求解器接口；未执行安装或数值验证。
- CVXPYlayers `pyproject.toml`，blob `f8a0c9c951634df6c029c52656e468b1a630c64f`：已核对 Python、CVXPY、diffcp 和可选 torch 依赖。GPU 能力须按具体后端实测，不沿用旧版本的笼统判断。

## 5. 吸收方式与研究优先级

| 优先级 | 决策 | 依据与产出 |
| --- | --- | --- |
| 首先 | 原始数值窗口 + 小型 GRU/ALSTM；保留 Ridge/LightGBM | P01/P02 与 Qlib；先验证时点、输入、预算、样本和标签公平性。小模型不是最终能力上限 |
| 首先 | 将真实持仓、现金、可售数量接入独立目标持仓契约 | 现有 Bench 只收预测分数；直接塞入“权重分数”会丢失策略含义，必须单独扩展 |
| 随后 | 相同编码器，预测损失 vs 净效用损失；相同组合层对照 | P03/P04；拆开表示增益、决策训练增益、风险层增益，避免混在一张收益图里 |
| 随后 | CVXPYlayers + 固定、正则化风险估计；实际账户独立验证 | P04/deepdow；不从零写微分求解器，也不一开始同时学习风险、成本和收益全部模块 |
| 条件满足后 | BPQP 后端、MASTER、适应性训练 | P05/P13/P14；先通过数值与经济一致性，再讨论加速或复杂性 |
| 条件满足后 | Kronos/Chronos 对照、公告文本 | P06/P07；预训练污染、公告修订、发布时间与授权先可追踪 |
| 暂缓 | RL、从零预训练基础模型、自治实盘 Agent | 成本与验证面过大；没有证据表明它们是本项目建立第一条可信链路的必要条件 |

树模型应继续增强：严格历史验证下的轮数与参数选择、标签/样本权重、缺失与市场状态处理、与相同组合层的配对对照。神经网络并非默认更优；表格特征路线成熟也不表示收益已经不可改善。具体提升须来自冻结协议下的实测，不能只凭架构新旧判断。

## 6. 公开发布应展示的证据

首个版本以“可复现研究 alpha”发布：干净环境安装、获授权的小型公开样本或明确标识的合成样本、固定协议、同预算基线、失败与负结果、完整试验台账、数据/模型卡、训练近似与账户差异报告。合成样本只证明软件和会计机制，不能证明真实市场效用。

已有本地开发实验、被反复查看的 2024–2025 历史数据及其后筛选结果，都不能重新命名为独立留出确认。先冻结下一次确认协议、版本与日期，再获取真正未用于选择的数据。数据与账户证据仍为 `DIAGNOSTIC_ONLY` 时，只能发布相应诊断，不能升级为经济有效性主张。

下一步应按 SPEC 的 M0–M5 逐项实施，而不是继续无边界叠加论文模型。
