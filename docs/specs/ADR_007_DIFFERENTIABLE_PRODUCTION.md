# ADR 007：训练与评价共用的 Tensor 生产映射

日期：2026-10-23。状态：**方案确定，工作区已有待验收实现；未通过本 ADR 门槛前不开放正式效用训练**。依赖：[ADR 005](ADR_005_PREDICTION_ALLOCATION.md)、[ADR 006](ADR_006_CONTINUOUS_ACCOUNT.md)、[N03 SPEC](N03_UTILITY_TRAINING_SPEC.md)。

实施补记：箱体/预算/换手、光滑分支方向导数、GRU 到跨日效用、严格精确目标已有定向验证，并完成仅限合成诊断的训练 smoke。全部消融和完整闭环差异门槛仍未验收；代理成本超限，见 [训练说明](../UTILITY_TRAINING.md)。

最新证据：已用 12 份相同原始解比较 strict_raw/HiGHS/旧 Tensor/新 Tensor，接受数分别为 0/12/12/12，并以两个固定 checkpoint 分别运行两种 Tensor 的三路径评价。新规则降低此案例的连续账户费用，精确账户路径结果不变，四组代理门槛仍超限；范围、路径及当前实现身份见训练说明和验证记录。该消融不代表真实市场验证，完整阶段仍未关闭。

## 问题

求解器容差内的极小负权重仍不符合严格目标契约。N02 已提供 `strict_raw` 和不参与反传的 HiGHS L1 投影；若训练只对原始解反传，评价却使用另一动作，不能称为完整生产路径梯度。

## 决策与适用范围

增加显式版本 `torch_box_budget_turnover_v1`，在 CPU float64 上对实际前向映射使用 PyTorch 分段自动微分。训练账户与精确账户调用同一映射，各自读取自身过去的实际状态。旧 N02 策略保留原语义；验证器始终只验证，不替生产方修复。

首版只支持逐资产上下界、股票总预算和 L1 换手上限。行业约束等额外耦合约束必须明确拒绝；不能因组合求解器支持该约束就认为生产映射也支持。扩展需另版本化并证明映射后仍满足全部约束。

输入为原始解 `z`、漂移实际权重 `h`、已知下界 `l`、上界 `u`、可配置股票预算 `B=1-receivable_weight-min_cash`、换手上限 `T`、最大修正 `epsilon` 与缓冲 `delta`。输入、参数、资产顺序均须有限且对齐；`0<=l<=u<=1`、`sum(l)<=B<=1`。应收和最低现金不可因映射被花费。

映射定义：

1. `B_safe = B - min(delta, (B-sum(l))/2)`；先取 `x=min(max(z,l),u)`，再令 `s=x-l`，将 `s` 按不超过 1 的共同系数缩放，使 `sum(l+s)<=B_safe`。`sum(s)=0` 单独定义，禁止除零。
2. 若 `||x-h||_1` 超过 `T_safe=T-min(delta,T/2)`，对 `h` 使用相同的箱体/预算映射生成锚点 `a`。锚点换手仍超限则结构化失败；不能承诺任何可行问题都能由此算法修复。
3. 对超限动作按 `alpha=(||x-h||_1-T_safe)/(||x-h||_1-||a-h||_1)`，取 `(1-alpha)*x+alpha*a`。箱体和预算可行域为凸集，L1 的凸性给出换手上界；浮点结果仍须复验。
4. 最终检查非负、上下界、预算、换手与最大单项修正。要求 `max(abs(w-z))<=epsilon<=1e-6`，`0<delta<=min(epsilon,1e-9)`；首个参考值 `delta=1e-12`。这是数值修正上限，不能修复实质性投资决策错误。

该算法不是最近点投影，也不保证得到与 HiGHS 相同的动作。缓冲造成不可用锚点时须失败，尤其是零换手或预算恰好贴住下界的场景。任何例外都不得放宽法律、资金和可售限制。

## 梯度与失败语义

梯度经过实际执行的箱体、预算和插值映射，保留对原始解、实际状态、预算与可微边界的有效路径。诊断序列化可以 detach；前向状态推进不能借用诊断值。禁止 straight-through 或用 HiGHS 前向配原始解反向。

箱体切换、零交易、预算切换、换手切换为分段边界，不承诺全局光滑或唯一导数。整数执行的零导数、市场限制硬门和最低佣金不连续性继续按 ADR 006 声明。有限的梯度也可能为零；报告可训练参数、梯度范数、零梯度比例和实际参数变化，不人为强制成交来制造信号。

非有限值、不可用锚点、超量修正或目标验证失败须保留原始解和失败原因。训练 trial 中止，不得悄悄续持并继续优化。独立评价如协议允许失败续持，必须显式入账、计入覆盖/失败率，并失去主要模型比较资格；两种失败处理不得混为训练成功。

每次决策记录原始/生产权重、最大及 L1 修正、缓冲、各约束残差、换手修正分支和映射版本；checkpoint 与运行配置包含同一版本/参数。精确目标继续绑定其原决策账户和协议身份。

## 验收与替代条件

| 场景 | 必须证据 |
| --- | --- |
| 箱体与预算 | 内点、不足预算、预算激活、零预算、零 slack、下界和预算相等；严格目标可接受 |
| 换手 | 内点、激活、零上限、已漂移到上下界外的旧持仓、可用及不可用锚点；失败不回退 |
| 数值与拒绝 | 微小负值、极小头寸、单项修正超限、非法 dtype/device/shape、NaN、额外耦合约束 |
| 梯度 | 各光滑分支的多步长差分；GRU 参数经求解/映射/跨日账户到效用；边界单列 |
| 同图行为 | 两种账户同状态得到同动作；不同执行反馈可合法产生不同的后续动作，差异完整保存 |
| 消融 | 冻结相同原始解比较 strict_raw/HiGHS/Tensor 的拒绝率、修正及动作；再单列闭环评价 |

差分门槛和会计精度沿用主 SPEC；新代码不继承旧实现身份下的通过结果。以上验收通过并有当前源码证据后，状态才能改为“已验收”。若误差/失败率不适合训练，保留失败与负结果，重新立 ADR 选择经验证的映射或求解后端。

## R03 边界用例索引

下面仅定位实际用例，最终通过状态与实现身份以 [阶段审查](N03_STAGE_AUDIT_20261023.md) 和 [验证记录](../VALIDATION.md) 为准。用例覆盖局部数值边界，不关闭代理经济资格。

| 本 ADR 场景 | 实际用例与解释 |
| --- | --- |
| 箱体/预算内点、激活、零预算、零 slack、下界等于预算 | `test_tensor_production_bounds_budget_and_audit` 的五组输入；包括微负原始权重和可修复超量；输出检查上下界、预算与修正 |
| 换手激活、零上限、漂移越上界、可用/不可用锚点 | `test_tensor_turnover_map_and_nonfeasible_anchor_fail_explicitly`；`test_zero_turnover_map_follows_actual_state_with_zero_raw_action_derivative` 单列状态梯度与原始动作零梯度 |
| 非法输入/物质修正拒绝 | `test_production_refuses_invalid_or_material_repairs`；负值、超量、shape、float32、NaN、反向上下界、负预算 |
| 设备与额外耦合约束拒绝 | `test_production_rejects_non_cpu_arguments_without_requiring_a_gpu` 逐一检查五个 Tensor 参数；使用 meta 而非 GPU 实测。`test_native_policy_rejects_extra_coupled_constraints_before_solving` 与配置拒绝用例覆盖行业上限 |
| 光滑分支和输入梯度 | `test_actual_production_map_directional_derivative_on_each_smooth_branch` 四分支多步长；`test_budget_and_actual_previous_state_keep_their_gradients`；不声称切换点导数唯一 |
| 模型到跨日效用 | `test_gru_solver_mapping_multiday_utility_directional_gradient`、`test_allocator_to_multiday_utility_gradient_matches_directional_difference`；受控非零梯度和差分 |
| 整数/零交易/最低费 | `test_integer_rounding_has_zero_gradient_and_detach_is_explicit`；`test_actual_fee_switch_and_zero_trade_discontinuity_keep_piecewise_gradients`；最低费光滑两侧三步长、零交易跳跃及切换点单列；ADR 008 用例覆盖极小目标 |
| 两账户同状态与因果反馈 | `test_native_gru_same_initial_account_uses_same_exact_and_continuous_action`；`test_native_gru_future_data_and_events_leave_prior_inputs_constraints_and_actions_unchanged` 的五类扰动；后续状态/审计可因已揭示执行合法分叉 |
| 失败原始解/上下文保留 | `test_native_training_faults_fail_worker_and_preserve_all_trial_states` 的 production 场景通过真实映射拒绝；保留原始分配、原求解审计与实际约束，首轮/完成一轮后失败均中止，无成功输出指针 |
| 固定原始解与固定 checkpoint 消融 | 历史 12 解及四组策略闭环已有独立归档，参见前述训练说明；当前核心需在阶段矩阵列出重放范围，不能直接升级历史归档身份 |

完整原生评价的逐日失败续持和较大资源规模仍分别开放；新增失败诊断不构成续持协议。当前支持范围仍为 CPU float64、箱体/预算/L1 换手和声明的日频多头研究。
