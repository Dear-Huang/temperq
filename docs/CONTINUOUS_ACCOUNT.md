# 连续账户与费用差异诊断

这是 [N03 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md) 的账户/效用基础；[GRU 效用训练](UTILITY_TRAINING.md) 已接入版本化配置、CLI/worker、逐轮候选审查与独立重算，已有本机合成运行。完整 N03 和代理一致性仍未通过。契约和梯度决策见 [ADR 006](specs/ADR_006_CONTINUOUS_ACCOUNT.md)，已实现共享组合层见 [预测配置](PREDICTION_ALLOCATION.md)。历史检查点保留原身份，最新状态见 [验证记录](VALIDATION.md)。

## 可运行的固定动作诊断

安装 [CPU 研究环境](E2E_IMPLEMENTATION.md)，先按共享组合层说明生成序列预测与配置演示，再执行：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_continuous_audit.py artifacts/allocation-demo-new/B0-ridge --output artifacts/continuous-audit-new
```

输入是冻结且完成的预测配置结果目录；输出必须不存在。数据搬迁后可用 `--data-dir <同内容PIT目录>`。脚本核验源结果清单，用当前独立 Bench 重放原目标，再重算原始决策状态的风险/成本配方；不会要求把旧结果的实现身份改写成当前身份。

它保留八种配置及失败，不重训或选择模型。`plan.json` 记录源码、环境、源产物清单、数据身份和全部方案；`attempts.json` 记录进行中/完成/失败/未运行；各模式 JSON 保存逐日动作、成交、费用、状态及会计残差；`cost_decomposition.json`、`result.json`、`report.md` 和 `checksums.json` 提供汇总与内容校验。取消时保留已写出的证据。

这些运行属于回看过结果后的软件诊断；不能当作事先冻结的经济确认。脚本不重新运行历史编码器/求解器，只检验冻结动作的账户和配方。

## 实现入口

| API | 作用 |
| --- | --- |
| `portfolio.continuous.ContinuousState.from_account` | 从原版本账户建立含现金、数量、估值、批次与应收的 CPU 双精度 Tensor 状态 |
| `portfolio.continuous.step` | 执行给定目标/保持股数动作，保持账户计算图；显式费用与执行松弛 |
| `portfolio.episode.run_episode` | 时间顺序推进，只有显式 `detach_every` 才截断状态；返回权益审计及遵守截断的逐日净收益 |
| `portfolio.episode.ContinuousIntent` | Tensor 目标或附原因的保持股数动作；可附因果费用系数 |
| `portfolio.recipes.estimate_tensors` | 因果历史风险/流动性配方，保留当前权益到成本系数的梯度 |
| `training.utility.net_utility` | 已扣费权益的平均对数增长与 ddof=0 净收益方差，不重复扣费 |
| `training.utility.net_utility_returns` | 直接使用 episode 逐日净收益，避免截断边界的旧权益分母梯度 |
| `portfolio.reconciliation.cost_decomposition` | 对同一组实际成交分解最低佣金、税率、滑点和代理冲击差异 |

这些是本地 Python 研究 API。目标 Bench 的公开 schema 保持不变；`qwb run` 的 `e2e_utility_v1` 已复用这些模块。恢复能力为整折重跑，不支持中途 episode 续训；保存优化器/RNG 不等于已支持断点恢复。

## 时序与梯度

策略只接收决策状态、当时资产池与已知事件，不接收未来执行行。开盘数据揭示后，旧持仓产生隔夜损益，目标按真实开盘权益变成数量，先卖后买，含费用约束资金，随后按收盘估值。目标等于收盘权重仍可能交易；`weights=None` 才是本 API 的保持股数控制，须声明原因。

`integer_lots=False` 放松份额与拆股整数性；`settlement=False` 放松交收；`market_restrictions=False` 放松停牌、限价和容量，但不能伪造缺失价格或允许退出池资产新增买入。费用可选择无费控制、已声明执行费（可切换最低佣金）或历史因果线性代理。

状态默认保留计算图，`numbers()` 只用于审计序列化，`state.detach()` 是显式训练截断。整数取整的局部梯度为零，硬门、最低费与零交易点需单列；没有 straight-through。目标非负要求不放宽，现有 HiGHS 生产投影不反传。后续 Tensor 生产映射与实际 GRU 到跨日效用已有受控梯度用例，仍不能推出所有边界可微或完整 N03 已验收。

## 费用明细版本

`cost_decomposition` 默认使用 `same_fill_cost_decomposition_v1`，保留旧字段。显式传入 `schema_version="same_fill_cost_decomposition_v2"` 才增加逐笔 `fills`、CNY 分项、`bp_of_initial_equity`、`bp_of_fill_notional` 与相对差。当前固定 checkpoint 执行诊断脚本在计划中声明 v2；旧诊断须按原脚本/实现核验。

初始权益取第一条精确账户审计的 `equity_before`，对所有日期和成交固定。每笔成交额 bp 用该笔 `notional_cny`；相对成本差用同成交代理成本。零成交额/零代理成本对应值为 null，绝对差保留。逐笔、逐日和总计分项均核对会计残差；意图到实际成交变化的影响与同成交费用模型差异分开报告。它是恒等分解，不是已校准成本模型或唯一因果归因。

## 已观察结果

29 日合成冻结动作诊断中，完整整数模式与独立原始股数账户的数量/交收相同，权益/现金最大差异约 `8.15e-10` 元。实际费用 250.0758 元，对同一组成交计算的代理为 68.3677 元；差额可解释为最低佣金溢价 192.7461 元，减去代理额外计入冲击的 11.0379 元。

改变为分数份额后，比例费用约 105.14 元，加入最低佣金后约 528.47 元；原整数模式约 250.08 元。分数股产生的微小交易也会触发最低佣金，不能仅凭“可微”假设代理与实际一致。保持股数控制费用为零，但其权益路径也不同于原目标再平衡。

这些是该历史检查点的 `SYNTHETIC_ONLY` 会计、计算图与诊断证据，费用误差的分解不等于预测模型或成本模型已经校准。生产映射、训练/选择和状态反馈的后续进度见 [效用训练](UTILITY_TRAINING.md) 与 [验证记录](VALIDATION.md)；整阶段评审及 N04/N05 仍开放。
