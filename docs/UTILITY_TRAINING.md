# GRU 连续净效用训练

`e2e_utility_v1` 已接入共享配置、CLI、worker、不可变运行归档与独立重算；本机合成运行通过。设计与关闭条件见 [N03 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md)，生产映射见 [ADR 007](specs/ADR_007_DIFFERENTIABLE_PRODUCTION.md) 和 [ADR 008](specs/ADR_008_NUMERICAL_TARGET_DEADZONE.md)。这条路径不要求先构造因子。完整 N03 未验收，代理差异仍超限。

## 当前链路

经过验证的 GRU 预测归档 → 同权重转 CPU float64 → 因果行情窗口 → 收益头 → CVXPYlayers → 显式 Tensor 生产动作 → 时间连续账户 → 已扣费净收益效用。每 epoch 从同一声明初始账户推进一次、更新一次 Adam；独立历史验证选 epoch，测试账户不参与选择。

风险/成本配方固定，未来成交数据只由账户环境揭示。最佳权重、优化器、CPU RNG 和 epoch 一起保存，安全重载后检查参数及动作路径。首版仅支持失败后整折重跑，不支持中途 episode 恢复。

## 共享 CLI/worker 运行

先安装 [CPU 研究环境](E2E_IMPLEMENTATION.md)，按 [序列训练](PIT_SEQUENCE_RUNS.md) 生成序列演示；也可新建一份源运行：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_sequence_demo.py --output artifacts/sequence-utility-source
$utilitySourceRun = (Get-Content -Raw artifacts/sequence-utility-source/comparison.json | ConvertFrom-Json).runs.'B1-gru'.output
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_utility_pipeline_demo.py --source-run $utilitySourceRun --bundle artifacts/sequence-utility-source/fixture --output artifacts/utility-config-new --epochs 2 --config-only
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli run artifacts/utility-config-new/utility.yaml --check
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli run artifacts/utility-config-new/utility.yaml
```

生成目录必须不存在。脚本仅用于合成数据，日历须覆盖零基索引 90（至少 91 个交易日，上述序列演示提供 110 日），冻结第 80–82 个零基索引决策训练、83–85 验证、86–88 测试、源 fold 0/seed 0 和诊断阈值；每段独立初始化首三个可见资产、各不超过 9%。这是固定软件场景，不能沿用为真实数据默认方案。不同源折可能因初始化晚于训练起点而被拒绝。

省略 `--config-only` 时，生成器直接调用同一 train worker，并在结束后执行扩展验证，结果存为生成目录的 `verification.json`。上面的显式 CLI 命令则将任务写到当前工作目录 `runs/jobs/`，成功归档存于 `runs/history/<run_id>/`，以实际任务输出为准。重新运行保留新运行身份，失败归档也不覆盖。`--check` 只解析配置；源 checkpoint、反馈可用性及内容身份的完整预检由 worker 在训练前执行。

取得实际运行目录后验证：

```powershell
$utilityRun = 'runs/history/<实际 run_id>'
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli runs verify $utilityRun
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli e2e utility-verify $utilityRun --inputs
```

第一条检查归档完整性，第二条用归档的控制/微调 checkpoint 重新生成三条账户路径、辅助预测与标签，并核对原输入身份。需要重定位数据时使用 `--data-dir`。这是动作和账户算术重算，不是重新训练复现；验证成功也不代表代理差异达标。旧 `run_utility_demo.py` 保留为低层 API 诊断入口，其历史产物不属于新的共享运行归档。

配置字段以 N03 SPEC 的 C1 为准。风险、成本、优化器、生产策略均放在 `portfolio` 下；生产段显式声明 `policy/buffer/dust_threshold`，容差在 `portfolio.optimizer.tolerance`。`continuous` 的五项开关、全部 `acceptance` 门槛必须明确填写。生成配置使用独立的极小目标策略，旧策略仍可选择且要求 `dust_threshold: null`。不能用测试结果事后放宽门槛。

`DecisionGRU.from_run` 核验源归档、数据内容、观察配方、fold/seed、权重及 fit 身份。任意权重路径的低层 `from_prediction` 只用于受控本地 API，不替代该来源验证。`validate_feedback` 要求历史成交价格来自截止时刻已可见的修订，不能仅凭成交日期早于截止就接纳尚未可用的价格。

## 产物与解读

| 产物 | 内容 |
| --- | --- |
| `task.yaml/config.resolved.yaml`、`protocol-fold-####-{phase}.json` | 任务、展开配置和归档协议；生成器另存运行前 `plan.json` |
| `data_manifest.json/observation_manifest.json/initialization.json` | 数据、观察配方及源预测归档身份 |
| `trials.json/folds.json` | fold/seed 全部尝试、逐 epoch 梯度/更新与验证、选择和失败/取消；未运行项保留 |
| `models/fold-####-seed-N/` | 已选 float64 权重、同一 epoch 的优化器/RNG、来源和训练记录 |
| `initialization/fold-####-seed-N/` | 复制源预测 checkpoint，支持同精度 B1 控制重算 |
| `accounts/fold-####-seed-N/B1-float64-control/` | 不做效用微调的同精度控制；不能用原 float32 B1 直接归因 |
| `accounts/fold-####-seed-N/B2-utility/` | 已选效用微调模型的相同评价 |
| 两组内的 `exact/` | 各自精确账户闭环生成目标，独立提交回放与输入重算 |
| `continuous.json`、`continuous-policy.json` | 各自连续账户闭环、状态、实际费用、动作、配方及映射审计 |
| `fixed-continuous-action-exact.json` | 冻结连续数值动作再交精确账户，重新绑定状态；仅用于执行近似诊断 |
| 两组内的 `differences.json/result.json` | 闭环与固定动作逐日差异、超限资格和结果 |
| `predictions.parquet/seed_predictions.parquet` | 实际可预测资产的辅助收益预测、均值及 seed 明细；不是目标权重，也不构造虚拟集成交易账户 |
| `metrics.json/report.md/run.json` | 总结、代码/环境、明确证据等级、可读报告与归档产物哈希；精确账户另有自己的 `checksums.json` |

任务成功只表示软件执行完成。`comparison_eligible=false` 和 `threshold_breaches` 表示代理差异超过事先声明的诊断门槛；不能据此将训练损失或连续账户收益放进主要模型比较。入口逐日检查权益元/bp、现金、应收总额、股数、按资产/UTC 到期时刻匹配的锁定股数、实际成交换手、动作、按资产/买卖方向匹配的未成交量及违约，另查总费用元/相对差。费用分母为零时相对差为 null，绝对费用门槛仍生效，包括等号边界。

新生成配置显式采用 `acceptance.state_comparison_version: lifecycle_schedule_v1`，额外声明四项权利/事件门槛，详见 [ADR 009](specs/ADR_009_LIFECYCLE_COMPARISON.md)。比较决策前及收盘后的逐事件数量、金额、归属/类型/确认阶段/UTC 支付日程和已处理事件集合；未确认、零金额但有正数量的权利也保留。旧配置不填版本或填 `aggregate_v1` 时保持原总额模式，不补新门槛或提升旧结果的验证范围。`metrics.json.state_comparison_version` 标明实际模式。

完整整数成交中的最低佣金与分数股微小成交不同。首次成功 smoke 中 B2 连续账户三日费用约 323.06 元、精确账户约 36.08 元，差约 286.97 元；B1/B2 都超限。精确净收益约 -0.03130% / -0.03468%，该短合成路径没有 B2 优势，也不能推断真实市场表现。全部结果保留，不改变阈值让其通过。

诊断发现，旧 B2 的 54 笔微小成交合计仅约 1.49e-8 元，却产生 270 元最低佣金。ADR 008 的新策略显式抑制权重不超过 `1e-10` 且下界为零的目标；不删除持仓或费用。正式 worker 的两轮合成运行选择 epoch 1，B2 连续费用约 67.27 元、精确约 39.82 元，费用差及其他状态指标仍超限；精确净收益 B1/B2 约 -0.03130%/-0.03471%。这次新训练和此前旧训练不是孤立的规则消融，应使用下节的固定 checkpoint 结果分析规则影响。

## 固定解与固定模型消融

在当前源码生成并验证一份效用归档后运行：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_utility_production_ablation.py --utility-run $utilityRun --output artifacts/utility-production-ablation-new
```

输出目录必须不存在。脚本只接受合成数据，先通过扩展验证，再冻结模型、协议、缓冲、阈值和接受门槛；不训练或选择新模型。每个 B1 控制/B2 checkpoint 分别以两种 Tensor 规则评价精确闭环、连续闭环与冻结动作执行；从旧 Tensor 路径保存的原始解及状态边界，额外比较 strict_raw、HiGHS 和两种 Tensor 映射。四种前向映射共享同一份原始解，不重新求解。映射结构检查不等于账户身份绑定；实际绑定由闭环独立回放另行验证。失败尝试保留，不平均成功子集。

当前合成运行 `artifacts/utility-production-ablation-20261023-001/` 的 12 份原始解中，strict_raw 全部因严格目标/约束检查拒绝，HiGHS 与两种 Tensor 均接受。四组固定 checkpoint 结果如下：

| 模型 | 旧 Tensor 连续费用/元 | 极小目标规则连续费用/元 | 两规则精确费用/元 |
| --- | ---: | ---: | ---: |
| B1 同精度控制 | 346.3199 | 156.3201 | 72.7482 |
| B2 已选效用模型 | 277.2737 | 67.2736 | 39.8218 |

同一 checkpoint 的精确净收益与费用不变，连续路径费用下降；这支持该规则在此案例减少数值微单费用，不是学习增益证据。四组代理比较资格仍全部为 false，整数手数、最低费与反馈差异仍未解决；也不能将此前两次不同训练间的全部差异归因于此规则。

`plan.json`、`attempts.json`、`frozen-map-comparison.json`、`result.json/report.md` 与逐模型/规则账户产物保留来源、checkpoint 身份、失败和结果。根 `checksums.json` 覆盖嵌套精确账户的 checksum 文件，不排除所有同名文件。

## 固定模型的执行误差归因

[接续 SPEC](specs/NEXT_ITERATION_SPEC.md) R01 的诊断入口：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_utility_execution_audit.py --utility-run $utilityRun --output artifacts/utility-execution-audit-new
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_utility_execution_audit.py --verify artifacts/utility-execution-audit-new
```

输出目录必须不存在。v1 仅接受 `continuous` 为分数份额、声明成交费用、交收/市场限制/最低佣金全部启用的源配置，其他模式明确拒绝。先验证源归档及原输入，然后冻结 checkpoint、数据、协议、生产配置、原接受门槛与诊断顺序，不训练、不选模型。实现和脚本身份分别记录；源码或脚本改变后旧诊断须在原实现中核验，不能重新解释。

每个模型保留三条原始评价路径，再固定连续闭环的数值目标，做整数手 × 最低佣金的 2×2 对照。额外单独禁用交收或市场限制，并加入保持股数控制；可买资金、价格、事件仍由账户推进处理。禁用机制的路径只是反事实，不能当作合法生产成交。

顺序桥接为：分数最低费 → 分数比例费 → 整数比例费 → 整数最低费 → 独立固定动作 → 独立自身状态闭环。每一步报告费用、期初/期末权益、隔夜/日内/公司行为盈亏的差额及恒等残差；最后一步体现策略读取不同历史账户状态后的反馈。顺序贡献不是唯一因果分配，整数手和最低佣金交互另报。

`artifacts/utility-execution-audit-20261023-002/` 的固定模型合成诊断结果：

| 路径 | B1 费用/元 | B2 费用/元 |
| --- | ---: | ---: |
| 分数最低费 | 156.320103 | 67.273597 |
| 分数比例费 | 21.752840 | 11.762142 |
| 整数比例费 | 14.042544 | 6.187488 |
| 整数最低费/独立固定动作 | 71.735652 | 38.593629 |
| 独立自身状态闭环 | 72.748224 | 39.821832 |

两模型的整数完整路径与独立固定动作逐日股数/锁定股数/未成交差异均为零，最大权益差约 `2.33e-10` 元。分数路径均在首个决策分叉；未修改其超限资格。该样例禁用交收或市场限制未改变成交，不能说明这些约束无影响；定向压力用例另验证限制确实能改变实际成交。

B2 在比例费下从分数改整数，费用减少约 5.57 元，但末期权益下降约 49.96 元；整数最低费到自身状态精确闭环的反馈使末期权益增加约 4.10 元。费用改善不能直接归为净收益改善。最低费的路径效应在分数/整数下约为 55.51/32.41 元；二者交互约 -23.11 元，不等于固定成交上的最低费溢价。

每个 `mechanisms/<scenario>.json` 保存完整前后状态、逐笔成交差异、首个观测分叉、同成交成本分项与 `actual-intended = (filled-intended)+(actual-filled)`。配方冻结在原决策状态，不在反事实路径上重新拟合。首分叉的元/股观察容差只用于定位，不替代源资格门槛。结果与计划、全部失败尝试、原精确账户嵌套 checksum 均入清单。

当前脚本显式声明 `same_fill_cost_decomposition_v2`：每笔成本分项同时给出 CNY、固定期初权益 bp 和本笔成交额 bp；逐日/总计也报告固定权益 bp。相对成本差以同实际成交代理成本为分母；零成交额或零代理成本对应 null，绝对差保留。桥接费用差也附固定权益 bp。旧 API 默认 v1、旧诊断脚本和历史归档保持原解释，不能用新脚本直接验证旧脚本身份的结果。

`--verify` 先核验文件和独立账户，再在临时目录重新生成完整诊断并比较计划与全部确定性产物；临时输入路径和 checksum 单独按原独立验证器核验。重算不是重训，也不说明 N03 已整体验收。原版本诊断、当前正式脚本诊断和最终回归分别保留，不累加测试批次。

## 失败上下文

原生训练的缺历史、求解失败、生产拒绝或非有限梯度会中止 trial；已经完成的 epoch/candidate 保留，未开始的折/seed 为 not_run，不生成成功运行的输出指针。`trials.json` 中的 `failure_context` 使用 `utility_training_failure_v1`，记录轮次、已完成轮次、训练阶段与原异常。

若错误来自策略决策，内嵌 `decision_policy_error_v1`，按阶段保留时刻、资产、快照、历史长度、预测表示、实际持仓约束及配方身份。生产失败额外保留原始分配和求解审计。具体字段以 [N03 SPEC C4](specs/N03_UTILITY_TRAINING_SPEC.md) 为准。该训练诊断不是目标提交中的显式失败记录。可选外层精确诊断续持按 [ADR 011](specs/ADR_011_OUTER_EVALUATION_FAILURES.md) 单独声明，配置、覆盖、资格和回放边界见 [外层评价](OUTER_EVALUATION.md)。

## 梯度、截断和内存

`run_episode(..., detach_every=K)` 在第 K、2K… 次决策之前仅截断状态图，保留持仓与交收/应收数值。训练使用返回的 `net_returns` 和 `net_utility_returns`，其分母是当日实际使用的期初账户权益。截断后不能用保存的整条 `equity` 相邻比值训练，否则可能从未截断的前日分母引入伪梯度。`equity` 仍用于数值审计。

首版汇总 episode 后一次反传，`max_episode_steps` 是硬上限。状态 detach 不保证内存随 K 有界；`memory.peak_rss_bytes` 是进程高水位，包含导入和此前 trial，不是单个 epoch 独占内存或 GPU 显存。本机首次 smoke 约 439.74 MB（十进制），其他机器和规模须实测。

有限零梯度与未胜出是合法结果；非有限梯度、非正权益、超量动作修正、来源/截止错误会中止。取整、最低佣金、活跃约束和市场硬门的梯度边界继续显式记录，不能声称全局光滑。

## 每轮内层验证审查

按 [ADR 010](specs/ADR_010_VALIDATION_CANDIDATE_AUDIT.md) 在 `e2e_utility_v1` 配置顶层显式加入：

```yaml
validation_audit:
  schema_version: utility_validation_audit_v1
```

只接受这一版本及字段。省略时不向旧 resolved config 增加默认键，保持原选择语义。合成生成器可用 `--validation-audit` 产生相同配置；例如给前述 `run_utility_pipeline_demo.py` 命令增加 `--epochs 3 --validation-audit`。配置和全部拟合/搜索预算仍需运行前冻结。

每个完成更新及连续验证的 epoch 都复制独立候选，保存同轮权重、优化器、CPU RNG 和训练记录；候选重载后无梯度运行内层 validation 三路径。审查器不接收外层 test 协议，反馈必须满足 `selection_cutoff`。连续重算损失与原该轮损失必须完全一致。成本已经扣除，精确净效用使用相同平均对数增长及总体净收益方差，不额外扣费。

| 归档 | 内容 |
| --- | --- |
| `validation-candidates/fold-####-seed-N/plan.json` | 内层协议/源码/数据/初始来源、配置、时间截止、原接受门槛及预算声明 |
| 同目录 `attempts.json` | 所有预声明 epoch；成功、失败、中断和未运行原因，不只保留最佳轮 |
| `epoch-####/checkpoint/` | `decision_candidate_fit_v1` 候选元信息、float64 权重及同轮训练状态；不是另一个已选模型 |
| `epoch-####/accounts/` | 相同候选的精确自身状态闭环、连续闭环、冻结连续动作回放与逐日差异 |
| `epoch-####/result.json` | 两类验证损失、净收益/方差/回撤、费用、实际换手、违约与代理资格 |
| `summary.json` | 原选中 epoch、连续/精确排序、逐对反转、Kendall tau-b、平分和完整性状态 |

仅按连续验证损失严格改善选 epoch、平分留较早轮，epoch 0 仍仅作控制。精确诊断不修改选择或早停，也不挑选通过代理门槛的子集。单候选或任一路全平分时 tau 为 null 并说明原因；失败候选存在时排序标明覆盖不完整。

审查及候选持久化计入原 `train.max_seconds`，逐决策检查取消/超时。普通诊断失败可保留原训练产物，审查状态为 PARTIAL；取消/超时继续使 trial 中止。`metrics.json.validation_audit_complete` 与训练成功、代理资格分别报告。诊断从独立副本执行，训练权重、优化器及 RNG 不从诊断结果更新。

`qwb e2e utility-verify <run> --inputs` 对启用审查的运行额外重载所有已完成候选，重算三路径、指标和排序，并核对候选/训练历史、已选参数及同轮优化器/RNG。缺候选、部分失败或修改结果后重哈希均不能得到完整审查 PASS。该验证仍不重训模型；后续改变选模标准须另立 ADR 和配置版本。

R02 历史正式合成运行 `artifacts/utility-validation-demo-20261023-002/runs/history/c4a3696edcdb4479b632a0122ccc9195/` 已执行三轮并通过扩展重算，所有候选审查完成，原规则选 epoch 3。内层结果如下，损失越小越好：

| Epoch | 连续验证损失 | 精确验证损失 | 连续费用/元 | 精确费用/元 |
| --- | ---: | ---: | ---: | ---: |
| 1 | -0.001211628718 | -0.001221058642 | 82.074897 | 48.716774 |
| 2 | -0.001211235208 | -0.001215000511 | 66.658108 | 43.119961 |
| 3 | -0.001212181634 | -0.001215000511 | 56.473970 | 43.119961 |

连续排序为 3/1/2；精确排序为 1/2/3，其中 2/3 精确损失平分。1/3 是反转对，Kendall tau-b 为 0。审查报告了这个分叉，没有事后改选 epoch 1。它证明代理排序可能影响选择，不证明改用精确验证一定能提高未见市场收益，也不允许查看外层测试后重新挑轮次。该运行的外层精确收益 B1/B2 约 -0.03130%/-0.03468%，两者代理资格仍为 false。

## 后续工作

R03 当前核心另有 `artifacts/utility-integrity-demo-20261023-001/runs/history/32a942d01fcd4ee0ae6978056fcc35dc/`，三轮/所有候选扩展重算 PASS，仍按原规则选 epoch 3，排序及反转结论相同；当前完整回归 525 passed。当前脚本的成本 v2 诊断在 `artifacts/utility-integrity-execution-20261023-001/`，完整重算 PASS，精确自身状态 B1/B2 分别 22/9 笔、费用 0.727482240/0.360843200 bp（固定期初 100 万元）；两组代理资格仍 false。Windows 个别进度事件追加遇锁转 stderr 的限制见验证记录，不能把计算图/归档通过解释为所有日志都已落盘。

当前核心的四映射消融为 `artifacts/utility-r03-production-20261023-001/`：12 份相同原始解，strict_raw 接受 0，其余三种各接受 12；两模型 × 两 Tensor 闭环均完成。B2 连续费用 268.0552 → 63.0552 元，精确费用/收益不变，两规则均不合格；不能将规则差异记成训练增益。冻结 276 文件源码包在独立 Windows CPU 环境安装并重训三轮，全部候选及所选参数、优化器/CPU RNG、预测/精确账户一致，报告 `artifacts/utility-r03-export-verification-20261023-001.json` PASS；数据/初始化仍来自工作区，资源工具不在该旧包内。

[资源实测](UTILITY_RESOURCE_AUDIT.md) 使用逐场景独立进程、真实预测源和固定矩阵，记录全体场景、失败及成功条件下的时间/RSS范围。脚本另有 17 项定向测试通过，包括真实预训练/效用训练、重复参数一致、超时回收 Windows 子进程树及重哈希后缺场景拒绝；不与核心 525 项相加，也不据 `detach_every` 声称有界内存。

正式矩阵为 24/64/128 资产 × 3/12 步 × full/2 截断 × 两次重复，24/24 完成且 12 组重复参数一致，溯源与汇总验证 PASS；fit 约 9.45–43.78 秒，进程高水位约 399–762 MiB。64/128 资产组梯度约 `1e-13`–`1e-11`，运行成功不等于有充分学习信号；后续 R04-D 先在开发数据诊断敏感度/约束/尺度，不调整本次冻结结果。完整范围和原始路径见资源说明。

T4/T5 已有配置/worker/三路径重算、应收日程与零分母/重哈希预测篡改证据；R03 增加真实原生故障归档、跨层未来扰动和原 N02 重放。外层失败续持已按 [N03 v0.9](specs/N03_UTILITY_TRAINING_SPEC.md) C4-F / ADR 011 在本机合成软件范围验收；当前完整主项目 584 passed，正式源预测/效用训练及失败恢复回放 PASS。训练/候选审查中止语义不变。完整阶段审查仍待完成，成本近似超限保持诊断资格。下一步按 [SPEC v1.4](specs/NEXT_ITERATION_SPEC.md) 做 R03-S，再诊断 R04-D 学习敏感度；历史四映射、资源矩阵和导出重训不改写身份。之后才进行符合资格的 N04 多折、多种子及 2×2 主比较。Linux、非实现者复现和服务器部署继续按 N05 验收，实际证据见 [验证记录](VALIDATION.md)。
