# 端到端改造进度与使用入口

依据：[实施 SPEC](specs/E2E_RESEARCH_PLATFORM_SPEC.md)、[ADR 001](specs/ADR_001_E2E_CONTRACTS.md)。本文描述实际代码进度；SPEC 中未实现的配置不能直接执行。

## 已落地的基础

| 入口 | 行为 | 当前边界 |
| --- | --- | --- |
| `qwb.portfolio.contracts` | 时点状态、可售/待结算股数、挂单、状态身份、目标持仓验证 | 仅多头、无杠杆；不是实盘接口 |
| `qwb.portfolio.reference.execute_session` | 旧持仓隔夜 → 开盘原始股数成交 → 费用 → 收盘估值；T+1、整数单位、成交限制/未成交原因与账本核对 | 开盘 IOC，暂不处理公司行为/现有挂单，始终诊断 |
| `qwb.observations.WindowBuilder` | 按 available_time 与修订构建原始数值窗口，缺失/资产池掩码 | 需要用户真实时点数据 |
| `qwb.folds.rolling_time_folds/time_fold_masks` | 月/周滚动或扩展窗口，按实际标签可用时间 purge | 新版预测运行已接入；旧年度路径保留 |
| `qwb.portfolio.optimization.ConvexAllocator` | DPP 风险/成本/稳定性组合层，原始解可行性验证、独立参考求解 | 可选依赖；须通过本机数值检查后才可视为可用 |
| `qwb.training.sequence.GRUPredictor` | CPU 小型 GRU、训练段标签尺度、历史验证早停、缺失掩码与安全 checkpoint 重载 | 已接入配置/CLI/worker/归档；无独立前向最终模型 |
| `qwb run` + `e2e_prediction_v1` | 时点 bundle、月/周折、Ridge/GRU、逐种子台账与预测、覆盖审计、内容变更拒绝 | 日频次日开盘至收盘预测；共享组合账户另走 bench allocate |
| `qwb bench allocate/allocation-verify` | 因果风险/成本、实际状态反馈、B0/B1 共用组合层、独立目标账户及全量重算 | 预测后配置，非效用训练；费用代理/精确成交有明显差异，需单独诊断 |
| `portfolio.continuous` + `portfolio.episode` | 保留计算图的跨日账户、显式执行松弛与状态截断、独立账户对照 | 内部研究 API；截断收益须使用逐日 `net_returns`；完整训练产品/worker 未接入 |
| `training.decision` + `training.utility_fit` | 显式 Tensor 生产映射、顺序 GRU 效用微调、历史选 epoch、同轮权重/优化器/RNG、安全重载与合成独立重放 | 本地 API/脚本；代理成本超限、完整差异门槛和配置/worker 尚未验收 |
| `qwb e2e check` | 手算账户/状态/分数隔离/求解与梯度的可执行验收证据 | 不是完整端到端模型或经济有效性认证 |
| `qwb bench targets/target-verify` | 独立目标提交、实际状态绑定、三日以上连续原始股数路径、冻结输入与完整重算 | v1 拒绝公司行为；v2 见下一行；跨日挂单拒绝，网页队列未接入 |
| `qwb.portfolio.lifecycle` + `target_protocol_v2` | 分红登记/除权/支付、不可花费应收、整数拆股与交收、终止索赔、事件损益核对；`target-fixture --lifecycle` 可演示 | v1 不变；仅声明扣税、残股拒绝、09:00 事件/15:00 登记；仍为诊断，跨日挂单不支持 |

新任务卡为 `examples/cn_alloc_daily_v1.yaml`：收盘后决策、次日开盘执行、次日收盘结束预测标签，组合账户另计旧持仓隔夜收益。可以用 `qwb validate task` 检查任务定义，当前旧训练器会明确拒绝其目标持仓输出，不能直接用于 `qwb run`。

独立预测任务为 `examples/cn_predict_intraday_v1.yaml`，可运行配置为 `examples/run_e2e_prediction.yaml`。数据格式、合成样本生成、三种子对照与复现命令见 [原始行情序列训练](PIT_SEQUENCE_RUNS.md)。因子仅属于可选 Ridge 对照，GRU 直接读取行情窗口。

目标持仓 JSON、合成示例与重放命令见 [目标持仓 Bench](TARGET_BENCH.md)。Ridge/GRU 冻结预测已通过 [共享组合层](PREDICTION_ALLOCATION.md) 接入该账户；连续效用训练尚未完成。

N01 的版本、事件单位、目标应收预算与会计决策见 [ADR 004](specs/ADR_004_ACCOUNT_LIFECYCLE.md)。v2 逐事件处理后仍由原始股数参考执行，目标绑定原决策状态；应收计入净权益但不会提供买入现金。

## 运行检查

不安装研究依赖也能运行账户、状态、窗口与折测试：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_portfolio_contracts.py tests/test_portfolio_reference.py tests/test_observations.py tests/test_time_folds.py
```

可选研究环境需要 Python 3.11；以下新目录只是示例，不覆盖原 `.venv`：

```powershell
uv venv artifacts/environments/e2e-clean --python 3.11
uv pip install --python artifacts/environments/e2e-clean/Scripts/python.exe -r requirements-e2e.lock --torch-backend cpu
uv pip install --python artifacts/environments/e2e-clean/Scripts/python.exe --no-deps -e . -e third_party/ashare-tcost
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli e2e check --output artifacts/e2e-check-001
artifacts/environments/e2e-clean/Scripts/python.exe -m pytest -q tests/test_portfolio_optimization.py
```

Linux 将 Python 路径改为 `bin/python`。Windows 改用 `requirements-e2e-windows.lock`（加 `--only-binary :all:`），全部来自预编译包，不需要 MSVC、Windows SDK 或 CMake：

```powershell
uv pip install --python artifacts/environments/e2e-clean/Scripts/python.exe --only-binary :all: -r requirements-e2e-windows.lock --torch-backend cpu
```

原因：diffcp 1.1.x 没有 Windows 预编译包，cvxpylayers 1.x 又要求 diffcp ≥ 1.1；最后一个带 Windows 包的 diffcp 1.0.23 按 numpy 1.x 编译，与 numpy 2 同用会崩溃。因此 Windows 组合为 cvxpylayers 0.1.6、diffcp 1.0.23、cvxpy 1.5.3、scs 3.2.7、numpy 1.26.4、scipy 1.13.1，其余与主锁一致；`ConvexAllocator` 对 0.1.x 没有 `solver` 参数做了回退（0.1.x 固定用 diffcp + SCS）。2026-10-29 在全新虚拟环境中安装 60 个包，`uv pip check` 通过，`qwb e2e check` PASS，全量测试 1129 passed。由 `requirements-e2e-windows.in` 生成。跨平台 CI 会显式安装并运行，CI 配置存在不代表本地已验证 Linux。GPU 研究另建环境并单独记录，不把 CPU lock 随意改为未记录的 CUDA 版本。

检查输出目录必须不存在，避免覆盖旧证据。`acceptance.json` 保存成功和失败，`checksums.json` 保存结果哈希。基础环境未装求解器时，账户检查可通过，数值检查明确失败，CLI 返回 1。

## 本机已取得的证据

2026-10-23，`artifacts/e2e-m0-windows-20261023-001/acceptance.json` 的本地参考检查为 PASS：闭式解最大误差 `2.27e-14`，独立 CVXPY 差异 `4.34e-12`，原始约束残差 `2.78e-11`，三个步长的梯度相对误差均低于 `1e-3`（最大约 `7.25e-5`），手算账户现金 990、净权益 10290、费用 10，账本残差为零。报告自带当次代码/环境身份，后续源码变化不回写旧证据。

独立环境复用了本机已有 CUDA 版 torch，但检查张量在 CPU 上运行，**不是干净 CPU lock 安装证明**。最新基础定向测试为 46 passed；GRU 与数值定向测试合计 7 passed，31.96 秒，含上游 CVXPYlayers/NumPy 弃用提示。此前主项目回归 301 passed、1 skipped 的求解器跳过，已由后续装依赖后的数值实测补充；这些重叠批次不相加。

M1 首轮集成测试 8 passed，72.17 秒；随后配置、归档、worker 与序列模型定向回归 33 passed，209.98 秒（包含重叠测试，且早于最终标签尺度变更，后续复查另记）。覆盖月/周折、晚到修订标签、未来价格扰动、配置拒绝、样本预算、输入中途变更、失败保留、摘要身份和 CLI 真运行。

最终标签尺度版本定向测试 12 passed，82.17 秒。随后从 `requirements-e2e.lock` 在全新 `artifacts/environments/e2e-clean/` 安装 58 个第三方包和两个本地包，无系统 site-packages、无原环境 `.pth`；PyTorch 为 `2.11.0+cpu`，`uv pip check` 通过。独立 `e2e-m0-windows-clean-20261023-001/acceptance.json` 为 PASS，相关测试 62 passed，94.65 秒（记录 `artifacts/e2e-clean-tests-20261023.xml`），有上游数值依赖弃用提示。diffcp 使用此前本机 MSVC/SDK 构建缓存 wheel；这是干净虚拟环境验收，不是未配置编译工具的新 Windows 机器证明。Linux 仍未实测，本机 WSL 组件未启用。

目标 Bench 与参考账户首批定向测试 25 passed，2.11 秒；覆盖状态/身份拒绝、完整决策覆盖、失败续持、退出资产池、未来执行限制、手算现金/T+1、交易单位、冻结输入与算术篡改。公司行为仍有明确拒绝测试，不能据此把完整 M2 标记完成。

最终追加缺失掩码隔离、第一决策保持初始状态身份之后，配置/序列/目标账户定向验证 45 passed，143.02 秒，记录 `artifacts/final-focused-e2e-20261023.xml`。最新 CPU 数值参考为 `e2e-m0-windows-clean-20261023-002`，完整目标 CLI 演示为 `target-fixture-20261023-002` 与 `target-result-20261023-002`；`target-verify --inputs` 为 PASS。旧示例不回写当前源码身份。

干净 CPU 环境完整回归为 336 passed、无失败，566.99 秒，记录 `artifacts/full-e2e-regression-20261023.xml`，35 条现有/上游弃用提示。完整回归启动后追加的掩码与初始身份细化，由上述最终 45 项覆盖；不将重叠批次相加。源码发布扫描为 228 个文件、0 错误，10 份主要文档本地链接检查无断链。

`artifacts/sequence-demo-20261023-003/comparison.json` 为冻结三种子/三折的合成软件对照：60 日窗口、24 资产、29 个测试日、696 条完全匹配预测，全部 18 次拟合及 checkpoint 重放完成。Ridge MSE `2.29498e-5`，GRU MSE `2.30104e-5`；平均 Rank IC 分别约 `0.02330`、`-0.02216`。它不证明 GRU 更优，也不证明真实市场技能。首次未标准化标签对照保留在 `sequence-demo-20261023-002`；遇到 Windows 临时文件锁而失败的尝试保留在 `sequence-demo-20261023-001`。原子写入已加有界退避重试，不能因同步客户端占用而静默丢台账。

## 2026-10-23 账户生命周期验证

N01 首轮生命周期实现的最终定向回归为 69 passed，4.96 秒，记录 `artifacts/lifecycle-final-focused-20261023-004.xml`。覆盖 v1 契约/账户/目标兼容，v2 分红三阶段、声明扣税、应收不能融资买入、拆股与 T+1、整数残股拒绝、终止减记与延迟结算、同刻支付、事件时点/单位拒绝、未来事件扰动、缺价时拆股/除权估值、新应收目标字段不能进入旧分数入口，以及 CLI 冻结重算。

当前源码的 v2 合成 CLI 产物为 `artifacts/target-lifecycle-fixture-20261023-003/` 与 `artifacts/target-lifecycle-result-20261023-003/`，`target-verify --inputs` 为 PASS。三次收盘净权益为 10895、11270、11444.74；第二日现金 490、应收 900、持仓 1900 股。只证明给定合成路径的会计与软件行为，不证明市场效用。`...001/...002` 保留为此前实现身份的历史证据，不回写。

干净 CPU 环境本轮完整回归 359 passed、无失败，572.73 秒，35 条既有/上游弃用提示，记录 `artifacts/lifecycle-full-regression-20261023.xml`。完整回归启动后细化了分红/拆股单位检查与旧分数入口隔离，并补交叉场景，由上述最终 69 项覆盖；重叠批次不相加。当前源码的 CPU 数值及手算入口 `artifacts/e2e-m0-windows-lifecycle-20261023-001/acceptance.json` 为 PASS，实现身份 `sha256:04c3fa487547a023a8c538386123668182dc3be03ed7c7437c9611938bbedc59`。

发布文件扫描为 231 个文件、0 错误；本轮六份文档 43 个本地链接无断链。新的源码交付目录为 `artifacts/releases/20261023-lifecycle-source/`；只包含源码/配置/依赖锁与文档，不分发本地数据、权重或环境。导出验收记录另存 `artifacts/lifecycle-release-verification-20261023.json`；旧发布包不覆盖。

## 2026-10-23 共享组合层与 N03 接续

N02 首轮实现包括因果日内风险/历史流动性成本、实际账户换手、应收预算、显式有界生产投影、逐决策失败账、独立账户提交与全量重算，设计见 [ADR 005](specs/ADR_005_PREDICTION_ALLOCATION.md)。最终合成演示 `artifacts/allocation-demo-20261023-004/` 中 B0/B1 各 29 个目标、无失败、无按权益阈值超限，两者实际成交路径相同，净收益约 0.3487%，费用 250.0758 元，实际累计换手约 0.057429。

成交代理费用为 68.3677 元，实际高出约 265.78%，意图代理几乎为零；不能把零阈值超限解释为成本相对准确。该案例为 N03 的漂移/最低费/手数及动作映射诊断输入，没有市场效用或模型优胜结论。首个 strict_raw 运行的全部决策被极小负权重拒绝，和后来的全现金负结果均保留；输出策略版本与梯度边界见 ADR 005。

最终补充前的针对性验证 62 passed，45.44 秒，记录 `artifacts/allocation-final-focused-20261023-001.xml`。最终实现的完整回归及发布验证另在 [验证记录](VALIDATION.md) 记载，不累加重叠测试数。

N02 检查点的完整回归 380 passed，无失败/跳过，620.47 秒，137 条既有/上游弃用提示，记录 `artifacts/allocation-full-regression-20261023.xml`。该检查点的数值/梯度/手算账户入口 `artifacts/e2e-m0-windows-allocation-20261023-001/acceptance.json` 为 PASS，实现身份 `sha256:0f8da5b4a36e99c487970151292fd0b7c41d418070b094058729ef0f3946accb`。源码交付目录 `artifacts/releases/20261023-allocation-source/`，独立导出安装及重算记录 `artifacts/allocation-release-verification-20261023.json`；历史包不覆盖。

## 2026-10-23 N03 连续账户基础

新增 [连续账户](CONTINUOUS_ACCOUNT.md)、[ADR 006](specs/ADR_006_CONTINUOUS_ACCOUNT.md) 及净效用基础函数；标量风险/成本历史配方保持原语义，Tensor 接口保留资本到流动性系数的梯度。已验证手算现金/费用、实际状态反馈、交收与事件路径、资金约束导数、CVXPYlayers 参数到多日效用的光滑方向导数，整数取整梯度明确为零。

最终定向回归 86 passed，67.49 秒，记录 `artifacts/continuous-final-focused-20261023-001.xml`，含新连续账户及原组合、生命周期、目标 Bench；不与历史完整回归相加。当前实现身份 `sha256:c5725aff81b0d3e99d88fa683f998fa6e16ddabcf65eb53fe80bcab68c6c4b53`。

冻结 Ridge 和 GRU 的诊断分别为 `artifacts/continuous-audit-20261023-002/`、`artifacts/continuous-gru-audit-20261023-001/`。原账户与因果配方重新核验，八种模式全部完成；完整整数模式的数量、交收日期/数量和应收差异为零，权益/现金最大差异约 8.15e-10 元。同成交费用差已解释为 192.7461 元最低佣金溢价减 11.0379 元代理额外冲击；分数股最低佣金模式仍与整数执行明显不同，固定动作诊断不替代闭环策略验证。

源码交付目录 `artifacts/releases/20261023-continuous-source/`，发布记录 `artifacts/continuous-release-verification-20261023.json`。N03 仍需生产动作/梯度关系、GRU 连续训练、训练配置/worker、恢复声明与独立账户状态反馈比较；当前没有新增真实市场经济结论。

## 2026-10-23 N03 本地 GRU 效用训练

新增 [训练说明](UTILITY_TRAINING.md) 与 [ADR 007](specs/ADR_007_DIFFERENTIABLE_PRODUCTION.md) 对应的本地实现。`DecisionGRU.from_run` 从验证过的源预测归档定位 fold/seed，检查同数据与观察配方；训练固定 Adam、顺序 episode、历史验证选择，保存最佳同轮权重/优化器/RNG。`run_episode` 返回遵守截断边界的逐日收益，避免旧权益分母产生伪梯度；仅声明整折重跑。

最终定向回归 117 passed，98.75 秒，记录 `artifacts/decision-final-focused-20261023-004.xml`，280 条上游弃用提示。包含新增 31 项及连续账户、共享组合、数值优化、生命周期、目标 Bench；不累加历史批次。当前实现身份 `sha256:d77c7643568d71f4484c653c2ec6d617a81b2b428ea7ac146642ea52c80bb089`。

最终本地 smoke 为 `artifacts/utility-smoke-20261023-002/`：用既有合成 GRU 归档 fold 0/seed 0 微调两轮，选第 2 轮，参数变化范数约 0.039599；两模型分别完成自身连续/精确账户反馈、精确目标独立重算和冻结连续动作的精确诊断。源 float32 权重先转 float64，B1 控制同样转换。首次与最终 smoke 的主要结果一致，没有根据测试重新选择方案。

三日精确净收益 B1 约 -0.03130%、B2 约 -0.03468%，不构成模型优势。B2 连续费用 323.0552 元，精确费用 36.0843 元，差 286.9709 元，相对约 795.28%；预声明权益和费用门槛均超限，`comparison_eligible=false`。费用代理仍是阻断项，不以完成训练替代准确性验收。进程峰值 RSS 首次约 439.74 MB，包含导入和此前操作，不是单 episode 独占内存。

接续 N03-T4/T5，补齐严格配置、CLI/worker、完整归档验证、实际换手/交收/动作反馈差异与资格；T1 的生产映射消融/边界和完整阶段评审仍待完成。完整回归、Linux、外部复现与服务器部署没有新增完成结论。源码检查点目录为 `artifacts/releases/20261023-utility-source/`，导出安装及重跑验证以 `artifacts/utility-release-verification-20261023.json` 的状态为准。

## 2026-10-23 N03 共享运行与差异门槛

本轮接通严格 `e2e_utility_v1` 配置、现有 `qwb run`/train worker、全部 trial 状态、源 checkpoint 复制归档、辅助预测及三路径评价。`qwb e2e utility-verify` 使用归档控制/微调权重重算动作、预测、标签与独立账户，并核对配置和模型集合身份；`--inputs` 额外检查原协议及源归档。验证不重新训练，普通 `runs verify` 仍只表示归档完整性。命令和完整字段见 [训练说明](UTILITY_TRAINING.md) 与 [N03 SPEC v0.4](specs/N03_UTILITY_TRAINING_SPEC.md)。

新 [ADR 008](specs/ADR_008_NUMERICAL_TARGET_DEADZONE.md) 为极小目标增加独立策略版本，保留旧 Tensor 策略。旧案例 54 笔微小成交合计约 1.49e-8 元却支付最低佣金 270 元；新规则只改变显式目标，不删持仓、不免除卖出费用、不放宽预算/换手或既有验收门槛。不同资本规模下人民币含义不同，不能当作通用交易规则。

正式合成运行 `artifacts/utility-pipeline-demo-20261023-001/runs/history/f68baf4a75af4d2f961e48040463aa98/` 已由持久 worker 完成两轮，选择 epoch 1；扩展验证 PASS，作用域为 `utility_policy_and_account_arithmetic_not_training_reproduction`，证据为 `SYNTHETIC_ONLY`。源码身份 `sha256:c1b4d65083fe74266a4712a9e057648360f8eb354d13166af7813456780fc19b`。

B1/B2 三日精确净收益约 -0.03130%/-0.03471%，精确费用约 72.75/39.82 元；B2 连续费用约 67.27 元。权益、费用、股数、换手和动作等门槛仍超限，两组 `comparison_eligible=false`，没有模型优势或经济有效性结论。费用下降涉及规则与训练共同变化，不能作为孤立归因；固定原始解和固定 checkpoint 消融仍待完成。

最新定向验证为 16 passed，32.79 秒，记录 `artifacts/utility-integration-20261023-002.xml`，覆盖 11 项运行集成场景及 5 项极小目标场景。包括真实小模型训练、CLI/worker、扩展重算、重新计算哈希后的差异篡改拒绝、取消与输入改变失败、已持有极小仓位正常付费和全现金零梯度训练。与既有测试有重叠，不累加。全量回归及最终数值检查结果另见 [验证记录](VALIDATION.md)。

最终完整主项目回归为 454 passed，0 失败/跳过，701.35 秒，记录 `artifacts/utility-full-regression-20261023-001.xml`；当前源码的 CPU 数值入口也为 PASS。本轮源码导出目录 `artifacts/releases/20261023-utility-pipeline-source-001/`，清单/内容核验记录 `artifacts/utility-pipeline-source-verification-20261023-001.json`；没有对新导出重新安装或训练，不沿用旧包的安装证明。

T4/T5 已有实现和运行证据，不能因此关闭整个 N03：应收比较目前是总额，权利/支付日程及已处理事件集合仍需独立门槛；零费用分母、辅助预测重哈希篡改的专门回归和 ADR 007/008 消融仍待补齐。Linux、外部复现、实际部署未完成；旧源码 ZIP 保留原身份，不能代表本轮源码。

## 2026-10-23 权利日程比较与生产规则消融

此检查点对应 [N03 SPEC v0.5](specs/N03_UTILITY_TRAINING_SPEC.md) 和 [ADR 009](specs/ADR_009_LIFECYCLE_COMPARISON.md)，源码身份 `sha256:46cc891cf26c6de7555530559c6c0b6f08d2d071c0315e2b7e5bd98e2d4eed58`。旧总额比较配置及历史产物保持原含义，新生成配置显式采用 `lifecycle_schedule_v1`，不为旧配置暗加门槛。

新增按事件匹配的权利数量、金额、资产/类型/确认阶段/UTC 支付时间及已处理事件集合比较，覆盖决策前和收盘后状态；不能跨事件相抵。连续账户只在正数有权股数时建立权利，零股数记录不再与精确账户分叉；正股数、零金额的合法权利仍保留。测试还验证零精确费用下绝对费用门槛生效，以及修改辅助/seed 预测并更新 checksum/版本后仍被 checkpoint 重算拒绝。

定向回归 `artifacts/utility-state-focused-20261023-002.xml`：94 passed，109.57 秒，312 条上游弃用提示；覆盖新比较及消融、真实 worker、GRU 训练/梯度、连续账户和极小目标规则。首次 52 项批次中，零费用夹具未触发整数成交，修正为明确的分数股成交后由最终批次覆盖；不累加重叠结果。

当前正式 worker 运行 `artifacts/utility-pipeline-demo-20261023-002/runs/history/626893c62a574d73ab56faf0b97389fd/`：两轮、选 epoch 1，新比较版本已入配置/metrics，扩展验证 PASS，代理比较资格仍为 false。精确收益/费用与前一同配置检查点相同，没有新增市场或模型优势结论。

新增 `scripts/run_utility_production_ablation.py` 和逐决策 `production_inputs`。冻结 12 份同一原始解，strict_raw 接受 0 份，HiGHS/旧 Tensor/新 Tensor 均接受 12 份；再对两个固定 checkpoint 运行两种 Tensor 及三条账户评价，全部四次完成。结果 `artifacts/utility-production-ablation-20261023-001/`：B2 连续费用 277.2737 → 67.2736 元，精确均 39.8218 元；B1 连续费用 346.3199 → 156.3201 元，精确均 72.7482 元。说明此案例的规则影响，没有训练增益归因；四组代理门槛仍超限。

当前 CPU 数值检查 `artifacts/e2e-m0-windows-utility-state-20261023-001/acceptance.json` 为 PASS。完整回归及源码导出安装/本机重跑的最终记录见验证文档，不能以旧源码包或上述定向批次代替。N03 整体评审、剩余执行近似和 N04/N05 门槛仍待完成。

最终完整回归 474 passed，0 失败/跳过，585.03 秒，记录 `artifacts/utility-state-full-regression-20261023-001.xml`；449 条既有/上游弃用提示。267 个发布文件扫描无错误，16 份文档的 137 个本地链接无断链。新源码交付目录 `artifacts/releases/20261023-utility-state-source-001/`；独立本机 CPU 环境的导出安装与同配置重跑以 `artifacts/utility-state-release-verification-20261023-001.json` 的实际结果为准，不等同于非实现者或未配置机器复现。

## 2026-10-23 R01 固定模型执行归因

按 [接续 SPEC](specs/NEXT_ITERATION_SPEC.md) R01，新增 `scripts/run_utility_execution_audit.py` 与定向测试。仅新增诊断脚本，没有改变 `src/qwb` 的训练、账户、选择规则或源码身份；该身份仍为上节的 `46cc891c...eed58`，脚本内容哈希单独冻结在计划中。旧源码包不包含本轮脚本，不作为本轮交付。

正式诊断 `artifacts/utility-execution-audit-20261023-002/` 对既有 B1/B2 checkpoint 冻结目标，完成两模型各七种连续机制场景及两条独立精确参考。四格整数手/最低佣金、交收/市场限制反事实、保持股数控制、逐笔差异和首分叉均保留；顺序桥接与逐日盈亏恒等可重算。所有反事实是诊断，不改变源运行资格。

B2 费用桥接 67.273597 → 11.762142 → 6.187488 → 38.593629 → 38.593629 → 39.821832 元。整数完整路径与独立固定动作在两模型下的数量/锁定数量/未成交完全一致，最大权益差约 2.33e-10 元。最后自身状态反馈边使 B1/B2 末期权益分别改变约 -31.51/+4.10 元；不能把整数执行的费用下降全当作净收益改善。详细命令、适用配置与结果见 [训练说明](UTILITY_TRAINING.md)。

完整诊断重算 PASS，记录 `artifacts/utility-execution-verification-20261023-002.json`；原源码/脚本与源归档身份严格核验。专门测试覆盖反事实失败保留、分叉/逐买卖方向成交、交互、盈亏残差、交收/停牌/资金约束压力，以及修改结果并重哈希后仍被算术重算拒绝。软件检查范围与最终数量见验证记录。

这使 R01 有了当前合成场景的可重算证据，不宣称真实数据上的普遍代理准确性。R02 每 epoch 内层精确验证排序审查、R03 完整阶段矩阵及 N04/N05 继续未完成，分数训练代理仍超限。历史诊断 001 与正式脚本 002 分别保留身份。

## 2026-10-23 R02 内层验证候选审查

按 [接续 SPEC R02](specs/NEXT_ITERATION_SPEC.md) 和 [ADR 010](specs/ADR_010_VALIDATION_CANDIDATE_AUDIT.md)，新增显式 `validation_audit.schema_version: utility_validation_audit_v1`。每个完成 epoch 保存独立候选、同轮优化器/CPU RNG，在唯一内层 validation 重载并运行既有三路径及相同净效用公式。连续重算损失与原训练验证损失完全一致；诊断副本不改变训练、早停或原连续验证选模，审查及持久化计入原训练时间预算。旧配置省略该段时不补默认字段。

候选失败/中断和未运行原因全部保留，训练成功、审查完整与代理资格分开。扩展重算检查所有候选的账户、指标/排序、历史覆盖以及已选权重/同轮状态；重哈希结果篡改仍被拒绝。另以真实零梯度平分触发早停，验证后续 not_run 与已完成轮次缺失拒绝。

本轮修正共享 JSON 原子写入的 Windows 临时路径问题：同目录使用短、独占 `.qwb-*.tmp` 名称，继续保留文件替换锁的有界重试。原实现将目标文件名和长 UUID 追加到临时名，在深层候选归档中可能超出 Windows 路径限制；最终定向测试和完整回归验证该修改。没有放宽目标契约或更改账户公式。

当前源码身份 `sha256:5c8457692e64f3f3633fdb61dfe797f15cc0c41e24a405533f5d7a0d79730462`。正式共享 worker 合成运行 `artifacts/utility-validation-demo-20261023-002/runs/history/c4a3696edcdb4479b632a0122ccc9195/` 完成三轮并通过全部候选的扩展重算；原选中 epoch 3。连续排序 3/1/2，精确排序 1/2/3（2/3 平分），1/3 是反转对，Kendall tau-b 为 0。精确费用各轮约 48.72/43.12/43.12 元，不能仅从连续损失改善判断精确效用改善；没有事后改选 epoch 1。

完整主项目回归 495 passed，0 失败/错误/跳过，831.52 秒，825 条既有/上游弃用提示；收集后追加的早停测试另有 1 passed，不并入该完整批次。当前 CPU 数值入口 PASS。具体命令、XML 和历史失败见 [验证记录](VALIDATION.md)。

当前源码交付目录为 `artifacts/releases/20261023-validation-candidate-source-001/`。清单与独立本机安装/逐候选重训结果由 `artifacts/validation-candidate-release-verification-20261023-001.json` 的实际 `status` 判定；只有 PASS 才能引用其中逐轮参数/优化器/RNG、指标排序、辅助预测和独立账户一致结论。这仍使用本机缓存 Windows wheel、既有源预测运行及合成数据，不是自包含公开样本、冷机器或非实现者复现。旧 `...001` 训练产物与旧包保留原身份。

R02 的本机小型合成软件范围已验收，分数代理资格仍 false，外层 B1/B2 精确收益约 -0.03130%/-0.03468%，没有市场优势结论。新增 [N03 阶段审查](specs/N03_STAGE_AUDIT_20261023.md) 将 G0–G3 逐项关联证据和未关闭项；原生 GRU 故障归档、跨层未来信息扰动、完整映射边界和资源规模仍需补齐，N03/N04/N05 未整体关闭。

## 2026-10-23 R03 原生可靠性与成本单位

当前核心身份 `sha256:73cb717fe7effe4b1095cd053b9baf3591a895656767aac7c22fe7e1d2769c9e`。按 [主 SPEC v0.9](specs/E2E_RESEARCH_PLATFORM_SPEC.md)、[N03 v0.7](specs/N03_UTILITY_TRAINING_SPEC.md) 和 [接续 v1.2](specs/NEXT_ITERATION_SPEC.md)，补齐了以下局部链路：

- 原生输入相关 GRU 在连续/精确账户的五类未来扰动：首决策观察/掩码/可用时间/已知事件、风险成本、锁定约束、预算、原始解及生产动作保持一致；可用后预测和账户反馈须能变化。初始账户有真实持仓且首动作非全现金。
- 训练循环及 worker 保存版本化失败上下文；缺历史、真实求解后端失败、实际生产映射超量拒绝、非有限反传梯度分别在首轮/完成一轮后注入。已完成候选保留、后续折 not_run，保留原异常与生产原始解，不生成成功模型或输出指针。配置/初始化预检仍沿用旧错误台账；原生评价逐日失败续持没有新增支持。
- 成本 API 默认 v1 保持兼容；显式 v2 增加逐笔 CNY、固定期初权益 bp、本笔成交额 bp 和相对差，逐日/总计单位、零分母及残差可核对。脚本在冻结计划声明版本并拒绝用新脚本身份验证旧归档。
- ADR 007 已逐行关联实际边界用例，补非 CPU 参数/行业约束拒绝、零换手实际状态梯度及最低费两侧多步长/零交易跳跃。meta 拒绝不是 GPU 验证，边界不声称导数唯一。

完整主项目回归 525 passed，0 失败/错误/跳过，890.50 秒，1179 条既有/上游弃用提示；8 项故障定向批次不与之累加。当前 CPU 数值入口 PASS，原 N02 固定目标八机制当前重放 COMPLETED，整数完整数量/交收差 0、最大权益差约 8.15e-10 元；没有改写历史初始化或旧结果身份。开发中发现诊断错误读取连续状态的快照字段，已改用数据包身份并由故障与完整回归验证；早期失败记录保留。

正式 worker 归档 `artifacts/utility-integrity-demo-20261023-001/runs/history/32a942d01fcd4ee0ae6978056fcc35dc/` 三轮及全部候选扩展重算 PASS，仍选 epoch 3；原连续/精确排序分叉再次保留。固定 checkpoint 执行诊断 `artifacts/utility-integrity-execution-20261023-001/` 和完整重算报告 `artifacts/utility-integrity-execution-verification-20261023-001.json` PASS，精确自身状态 B1/B2 分别 22/9 笔、费用 72.748224/36.084320 元，分数代理资格仍 false。

Windows 个别进度日志追加遇锁后转 stderr，trial/candidate/模型/账户独立归档仍可核验；不声称每条进度均持久化，后续运维保留该改进项。运行与回归短时并行，时间/RSS 不是独立资源规模测量。此批源码包 `artifacts/releases/20261023-decision-integrity-source-001/` 先作库存/ZIP 内容核验，结果见 `artifacts/decision-integrity-source-verification-20261023-001.json`；随后独立安装/重训另记如下，不混用验证范围。

[阶段矩阵](specs/N03_STAGE_AUDIT_20261023.md) 保留未关闭项，按 [接续 SPEC v1.3](specs/NEXT_ITERATION_SPEC.md) 的 R03-R/F/S 继续。N03/N04/N05、市场优势及实际部署未关闭。命令、失败记录和证据边界见 [验证记录](VALIDATION.md)。

## 2026-10-23 R03 资源工具与当前核心复现

当前 `src/qwb` 核心仍为 `73cb717f...69c9e`，本轮没有改变原训练、选模或账户计算。当前四映射正式消融 `artifacts/utility-r03-production-20261023-001/` 已完成 12 份冻结解和四组固定模型闭环；strict_raw 拒绝 12 份，其余三种各接受 12 份。极小目标规则降低连续费用，没有改善此样例精确收益，代理资格仍 false。

冻结 276 文件核心导出在 `artifacts/environments/decision-integrity-export-20261023-001/` 完成缓存依赖安装、导出路径导入检查及三轮逐候选重训，报告 `artifacts/utility-r03-export-verification-20261023-001.json` PASS。已选/所有候选参数逐 Tensor 一致，优化器/CPU RNG、排除时间/RSS 后确定性记录、预测及独立账户一致。仅证明同机 Windows CPU 合成配置；源数据与初始化来自工作区，Linux/GPU、冷机器和非实现者尚未复现。

新增 `scripts/run_utility_resource_audit.py`、定向测试与 [操作说明](UTILITY_RESOURCE_AUDIT.md)。工具在预训练前冻结 intent，真实预测 pipeline 为各池生成源 checkpoint，核验后冻结 plan；每次效用 fit 在顺序新子进程完成，保存协议、输入、history、checkpoint、进程报告与所有失败。Windows 超时回收整个自有进程树，避免只杀虚拟环境启动器留下实际解释器。

最终定向批次 `artifacts/utility-resource-focused-20261023-003.xml` 为 17 passed，控制台 88.14 秒；包含真实预训练/重复 fit、原生 fit 预算失败、源准备失败、子进程树超时清理及重哈希删场景/改汇总拒绝。该批次与核心完整回归 525 项分别记录，不合计为一次完整回归。历史 001 的两项失败来自 Python tuple 与 JSON list 的原始协议比较；改为规范 JSON 身份比较后，002/003 通过，旧失败记录保留。

该资源工具不在此前安装的 276 文件包中。脚本独立验证其溯源与汇总，不重新训练或认证硬件计时，也不授予容量、GPU、市场效用或随截断长度有界内存的声明。后续原生精确诊断失败续持范围已写入 [N03 v0.8 C4-F](specs/N03_UTILITY_TRAINING_SPEC.md)，仍须新 ADR、实现与正式验收；当前并未开放。

正式资源矩阵 `artifacts/utility-resource-audit-20261023-001/` 为三个资产池 24/64/128、训练 3/12 步、不截断/每 2 步截断、每组两次，共 24/24 完成；全部 12 组重复参数相同，失败/超时/not_run 为 0。fit 9.454–43.782 秒，整个子进程峰值 RSS 418205696–798928896 字节，包含导入/加载。测量期间没有其他训练/测试/安装/基准，既有空闲本地服务保留，不宣称硬件独占；验证报告 `artifacts/utility-resource-verification-20261023-001.json` PASS。

64/128 资产组梯度仅约 `1e-13`–`1e-11`、参数更新很小；没有改配方强迫交易或用微小非零值声称有效学习。后续按接续 SPEC R04-D 做开发期动作敏感度/约束/数值尺度诊断，再冻结研究。R03-R 当前测量范围已有证据，R03-F/S 与经济资格仍开放。新源码包 `artifacts/releases/20261023-resource-audit-source-001/` 的库存/ZIP 核验另见 `artifacts/utility-resource-source-verification-20261023-001.json`，不宣称该新增工具已在导出环境安装重训。

## 2026-10-23 R03-F 显式外层失败续持

本轮核心 `sha256:86eb170d21c70654a2b2e5985209dc1c4f9ce0fa6def695e55f16cd46c088f94` 按 [ADR 011](specs/ADR_011_OUTER_EVALUATION_FAILURES.md) 实施可选外层 `utility_outer_evaluation_v1`。省略配置仍沿用旧评价；训练、连续 validation 和 ADR 010 候选审查继续中止。仅五类明确分类的策略失败可输出既有 `decision_failure_v1`；取消、系统/内存、预算、身份/配置、程序和会计错误不转成续持。

共享 evaluator 复用独立目标账户，失败日不生成再平衡订单，照常推进股数、交收、公司行为、应收与估值，下一日从真实状态重新决策。归档完整计划、实际参数/标签缩放身份、决策尝试和完整提交。连续诊断只保留成功前缀、首失败及后续 not_run，完整收益和缺失差异为 null，比较 INCOMPLETE。worker succeeded、训练成功、外层 DIAGNOSTIC_WITH_FAILURES 和比较资格分别记录；全部精确失败时辅助预测为空，不填零。固定模型归因/消融明确拒绝不完整评价。

最终定向 105 passed；完整主项目回归 584 passed，0 失败/错误/跳过，1203.12 秒，两批不累加。自包含 `artifacts/utility-failure-demo-20261023-001/` 在源预测预训练前固定缺失 high，完成预测一轮、效用两轮、全部候选审查及扩展验证 PASS。两模型各三个精确日期为成功/失败/恢复，连续各一完成/一失败/一未运行；资格 false，B2 未胜出。CPU 数值参考 PASS；命令、源码包和证据范围见 [验证记录](VALIDATION.md)。R03-F 仅在本机合成软件范围验收，归档事实回放不证明历史临时故障真实发生；当前核心也不继承旧包安装重训或资源计时结论。Windows 进度事件追加遇锁的已知降级仍出现，结构化归档已核验。操作见 [外层评价](OUTER_EVALUATION.md)，后续 R03-S、R04-D、N04/N05 继续按阶段矩阵推进。

## 后续完成门槛

M0 的干净 Windows CPU 环境与本地参考已验收，Linux 实测和完整契约验收仍待完成。M1 的预测软件闭环已接入，方案见 [ADR 002](specs/ADR_002_SEQUENCE_RUNS.md)；v2 生命周期见 ADR 004，仅覆盖明确声明的事件与税额。N02 共享组合层已接入，下一步按 [N03 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md) 先验收账户/代理一致性，再做连续效用训练、冻结研究比较与完整阶段评审。原始数据时点及外部复现没有证据前，不标记完整阶段通过。
