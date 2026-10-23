# 外层精确评价的失败续持

本功能对应 [ADR 011](specs/ADR_011_OUTER_EVALUATION_FAILURES.md) 和 N03 C4-F；只影响显式启用的外层诊断。训练、连续 validation 和 ADR 010 候选审查继续遇错中止。实际测试与正式运行结果见 [验证记录](VALIDATION.md)，完整阶段状态见 [阶段矩阵](specs/N03_STAGE_AUDIT_20261023.md)。

## 配置与运行

在已有 `e2e_utility_v1` 配置增加：

```yaml
outer_evaluation:
  schema_version: utility_outer_evaluation_v1
  failure_policy: explicit_failure_hold_v1
  max_seconds: 120
```

```powershell
qwb run path/to/utility.yaml --check
qwb run path/to/utility.yaml
qwb e2e utility-verify path/to/archived-run --inputs
```

省略此段保持旧评价行为，未知字段/版本/模式拒绝。`max_seconds` 是每个模型完整外层诊断的合作式预算，在决策与阶段边界检查，不能抢占正在执行的求解器；进程墙钟硬上限须由外部任务管理提供。价格修订和事件使用已有 `evaluation_asof` 截止验证，不能默认收盘价在收盘那一刻已发布。

独立合成演示会先固定缺失输入，再进行真实预测预训练和效用训练，不需要此前的本地模型产物：

```powershell
python scripts/run_utility_failure_demo.py --output artifacts/utility-failure-new
```

先按 [效用训练](UTILITY_TRAINING.md) 安装 `requirements-e2e.lock`，再使用该环境的 Python；输出目录必须不存在。本机验证使用的具体环境路径见验证记录，不随源码分发。演示为 110 日、24 资产、seed 17；一个资产在 calendar[87] 缺少 `high`，在预训练前写入源数据。预测 GRU 一轮、60 日窗口；效用训练两轮，train [80:83]、validation [83:86]、test [86:89]，seed 0。外层中日出现声明缺失，精确账户该日续持、最后一日重新决策；连续诊断在缺失日中止，不伪造最后一天收益。完整意图、源配置、worker 和验证记录留在输出中。它是工程故障演示，不是市场实验。

## 行为与资格

| 条件 | 处理 |
| --- | --- |
| 当前 OHLC/历史不足、因果风险/流动性历史不足 | 原生策略显式分类后，可在精确诊断输出当日失败记录 |
| 非有限收益预测、可识别求解失败或生产数值拒绝 | 同上，保留错误阶段、原始原因和已计算的审计 |
| 形状/dtype/device 错误、无效身份/配方/账户、目标验证、未知程序错误 | 中止，不据错误发生阶段自动续持 |
| 取消、超时、系统/内存错误 | 中止整个任务，不包装成成功决策 |
| 合法目标没有实际成交 | 仍为目标成功；拒单/未成交与模型失败分别统计 |

精确失败使用既有 `decision_failure_v1` 和独立原始股数账本。失败日没有新再平衡订单；既有持仓仍经过估值、交收、分红/拆股、应收支付。下一决策读取推进后的真实状态，不复用过期目标，也不把漂移权重当作“保持股数”目标。

完整精确路径的全部声明日期计入收益与风险。任一显式失败都使主要比较资格 false，结果为 `DIAGNOSTIC_WITH_FAILURES`。worker/运行归档的 succeeded 仅表示任务及产物生成完成；trial 另记 `training_state` 和 `evaluation_status`，CLI/报告显示外层状态和失败数。训练成功不能被解释为外层决策全部成功。

连续诊断失败时保留此前完整步骤和首失败上下文，后续日期为 not_run。完整连续收益为 null，三路径比较为 INCOMPLETE，差异为 null，不报告成功前缀均值来冒充完整表现。若连续完整、精确某日失败，则全部账户差异仍可计算，失败日的动作差异为 null，比较资格保持 false。

## 产物与验证

每个 `accounts/fold-…/B1…` 或 `B2…` 目录包含：

| 产物 | 用途 |
| --- | --- |
| `evaluation-plan.json` | 配置、核心、数据、协议、模型版本/配方、实际参数和标签缩放身份及反馈截止 |
| `decision-attempts.json` | 全部精确决策的上下文身份、成功或显式失败及诊断 |
| `policy.json`、`submission.json`、`exact/` | 成功动作审计、完整目标/失败提交与独立实际账户 |
| `continuous-progress.json` | 已完成连续步骤的前后账户、意图和审计 |
| `continuous-failure.json` | 仅在连续策略失败时存在，记录首失败及上下文身份 |
| `continuous.json`、`fixed-continuous-action-exact.json` | 仅在连续路径完成时存在，不给缺失路径生成空成功文件 |
| `differences.json`、`result.json` | 完整日期覆盖、比较状态、null 差异、收益/费用与资格 |

扩展验证先核验计划及实际模型身份，全部失败也不能替换模型参数或标签缩放；在成功日期重新执行实际模型，在失败日期读取归档错误事实，检查重新计算的上下文，重新生成显式失败提交。连续失败处同样检查上下文并停止，之前完整步骤重算。独立目标 Bench 重新推进全部日期，核对状态、会计与汇总。全体精确动作失败时，辅助预测文件可为空，但不得填零预测；完整日期仍保留在账户和失败台账中。

固定模型执行归因与生产映射消融要求完整外层评价，输入为失败诊断时会明确拒绝，须另建符合条件的完整运行后再使用这些脚本。

删除失败日期、改绑定、把单个失败提交改写成成功或把未完成状态改成完成，都不能靠重新计算文件哈希通过扩展重算。但回放不证明某次临时故障在历史上确实发生，也不能识别实现者一致改写整套错误事实。新旧归档的实现身份分别核验；不使用新代码声称旧身份下重新运行过。模型优势、代理资格、Linux/外部复现及部署仍有独立门槛。
