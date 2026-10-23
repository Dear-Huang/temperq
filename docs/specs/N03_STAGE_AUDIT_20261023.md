# N03 阶段证据审查

日期：2026-10-23。状态：**R03-F 本机合成软件范围已验收，R03-S 开放，完整 N03 未关闭**。依据 [N03 SPEC v0.9](N03_UTILITY_TRAINING_SPEC.md)、[主 SPEC](E2E_RESEARCH_PLATFORM_SPEC.md) 和 [接续 R03](NEXT_ITERATION_SPEC.md)。这是条目证据与差距清单，不是完整阶段通过证明。

当前 ADR 011 实现身份为 `sha256:86eb170d21c70654a2b2e5985209dc1c4f9ce0fa6def695e55f16cd46c088f94`。E1–E7 保留 R02/更早历史身份，其中 R02 为 `5c845769...30462`；E8–E17 条目中“当前”指冻结时的历史核心 `73cb717f...69c9e`，不代表 ADR 011 核心已重新测量、安装或重训。下表按引用的证据 ID 区分身份，当前核心新增证据从 E18 起单独记录。本机命令、完整回归的实际结果和历史身份见 [验证记录](../VALIDATION.md)。测试名只用于定位用例，必须同时核对对应身份下的最终结果。

## 证据入口

| ID | 当前证据及适用范围 |
| --- | --- |
| E1 | `artifacts/e2e-m0-windows-validation-candidate-20261023-001/acceptance.json`；R02 历史 CPU 小型账户/求解/光滑梯度 PASS，不代表全阶段 |
| E2 | `artifacts/utility-validation-demo-20261023-002/runs/history/c4a3696edcdb4479b632a0122ccc9195/`；三轮、单折/seed、110 日/24 资产合成源，内层候选与测试三路径，扩展重算 PASS |
| E3 | `artifacts/validation-candidate-full-regression-20261023-001.xml`；495 passed，0 失败/错误/跳过，831.52 秒；不含收集后追加的 E6 |
| E4 | R01 的 `artifacts/utility-execution-audit-20261023-002/`；旧核心身份 `46cc891c...eed58` 下固定模型归因与完整重算，当时脚本机制测试由 E3 覆盖；当前脚本另见 E9/E12，不改变旧归档身份 |
| E5 | `tests/test_validation_audit.py`、`tests/test_utility_runs.py`；候选/RNG/预算/取消/外层执行扰动与篡改；最终 E3 覆盖范围为准 |
| E6 | `artifacts/validation-candidate-earlystop-20261023-004.xml`；完整回归收集后追加的独立早停用例，1 passed；不计入 E3 的测试数 |
| E7 | `artifacts/validation-candidate-release-verification-20261023-001.json` PASS；R02 历史身份的源码/ZIP 核验、隔离安装与全部候选同机重训；不是当前核心安装、Linux/外部复现或公开数据包 |
| E8 | `artifacts/decision-integrity-failures-20261023-005.xml`；当前核心四类故障 × 两个失败轮次，8 passed；与完整回归不累加 |
| E9 | `artifacts/decision-integrity-full-regression-20261023-006.xml`；当前完整回归 525 passed，0 失败/错误/跳过，890.50 秒，1179 条既有/上游弃用提示；包含 22 项原生链路与 7 项成本分解，不与 E8 或历史批次相加 |
| E10 | `artifacts/e2e-m0-windows-decision-integrity-20261023-001/acceptance.json` PASS；当前核心手算账户、求解参考及三个光滑差分步长 |
| E11 | `artifacts/utility-integrity-demo-20261023-001/runs/history/32a942d01fcd4ee0ae6978056fcc35dc/`；当前核心三轮单折/seed 合成 worker 及全部候选扩展重算 PASS，原选 epoch 3，代理资格仍 false |
| E12 | `artifacts/utility-integrity-execution-20261023-001/`；当前核心/脚本的固定模型成本 v2、七机制与两精确路径，全部完成；完整重算 `artifacts/utility-integrity-execution-verification-20261023-001.json` PASS；不是重训或经济确认 |
| E13 | `artifacts/continuous-r03-replay-20261023-001/`；原 N02 冻结目标在当前核心八机制重放 COMPLETED，原独立账户 MATCH、原因果配方 RECOMPUTED；不重训历史源模型 |
| E14 | `artifacts/utility-r03-production-20261023-001/`；当前核心固定 checkpoint、12 份相同原始解的四映射消融 COMPLETED；strict_raw 接受 0/12，其余三种各 12/12；两模型 × 两 Tensor 闭环均完成且代理资格 false；不重训或改选模型 |
| E15 | `artifacts/utility-r03-export-verification-20261023-001.json` PASS；冻结的 276 文件当前核心包在独立 Windows CPU 环境安装、三轮逐候选重训，参数/优化器/CPU RNG/确定性记录/预测/独立账户一致；同机缓存依赖，初始化及数据来自工作区，不含随后新增的资源工具 |
| E16 | `artifacts/utility-resource-audit-20261023-001/`；24/64/128 资产 × 3/12 步 × full/2 截断 × 两次重复，真实预测源均成功，效用 fit 24/24；12 组重复参数相同；资源溯源/汇总验证报告 `artifacts/utility-resource-verification-20261023-001.json` PASS；不是训练重跑、容量认证或市场证据 |
| E17 | `artifacts/utility-resource-focused-20261023-003.xml`；新脚本独立定向批次 17 passed、控制台 88.14 秒；真实预训练/重复 fit/训练预算失败/源失败/Windows 整树超时回收及重哈希删场景/改汇总拒绝；不与 E9 合计 |
| E18 | ADR 011 当前核心 `artifacts/outer-evaluation-focused-20261023-005.xml`；105 passed，374.51 秒；实际策略五类故障、恢复/会计/连续中止、系统异常不吞掉、模型身份、worker 全失败空预测、整档重哈希篡改拒绝及原训练中止；不与历史批次累加 |
| E19 | `artifacts/utility-failure-demo-20261023-001/`；当前核心自包含源预测一轮、效用两轮及全部候选/账户扩展验证 PASS；两模型各 3 日期为 target/failure/target，连续各 1 完成/1 失败/1 未运行，资格 false；不证明市场效用或临时故障真实发生 |
| E20 | `artifacts/e2e-m0-windows-outer-evaluation-20261023-001/acceptance.json` PASS；当前核心 Windows CPU 手算账户、求解参考和三个光滑差分步长，不代替完整阶段 |
| E21 | `artifacts/outer-evaluation-full-regression-20261023-001.xml`；当前核心完整主项目 584 passed，0 失败/错误/跳过，控制台 1203.12 秒；1669 条既有/上游弃用提示；与定向/历史批次不相加 |
| E22 | 源码交付 `artifacts/releases/20261023-outer-evaluation-source-001/`；清单/ZIP 内容核验入口 `artifacts/outer-evaluation-source-verification-20261023-001.json`；仅内容库存，不含当前核心导出安装或重训结论 |
| E23 | 当前源码包普通安装/自包含重训报告 `artifacts/outer-evaluation-installation-20261023-001.json`；284 文件/ZIP CRC 与 E22 一致，独立环境 `site-packages` 导入，重新生成合成数据并完成源预测预训练、效用训练和全部候选审查；源预训练与效用候选参数、优化器/RNG、确定性记录、辅助预测和独立账户结果按比较范围一致；范围为同机缓存 Windows CPU、单折单 seed，不是冷机/Linux/外部复现或部署 |
| E24 | 最新文档同步 source-only 包 `artifacts/releases/20261023-outer-evaluation-source-002/`；`artifacts/outer-evaluation-source-verification-20261023-002.json` PASS，284 文件、ZIP CRC、workspace/stage/ZIP 内容一致，22 份 Markdown 的 230 个本地文件链接通过；仅内容交付核验，代码身份不变，不替代 E23 的安装/重训 |

以下“有证据”指列明的局部范围，不等于整条已关闭。“部分”明确留下需完成的验收；本轮不使用“不适用”来省略首版要求。

## G0：会计与动作

| 条目 | 实现与用例 | 证据判断和剩余工作 |
| --- | --- | --- |
| G0-01 无费分数份额手算 | `continuous.step`；`test_hand_account_keeps_old_shares_overnight_and_new_shares_intraday_without_double_fees` | 有手算及日内/隔夜恒等用例，当前数值参考 E10，广泛用例核对 E9 |
| G0-02 漂移与保持股数 | `test_target_old_weight_rebalances_after_gap_while_hold_control_really_keeps_shares`；R01 hold control | 有两种语义分离，不能将不交易控制当作目标权重语义 |
| G0-03 比例/最低费与资金 | `test_fractional_funding_pays_fees_once_and_does_not_borrow`、`test_minimum_fee_blocks_trade_if_cash_cannot_fund_it` | 有前向/资金/只扣一次证据；分数代理仍超限 |
| G0-04 手数/交收/市场限制 | `test_integer_tensor_account_matches_independent_reference_under_execution_restrictions`、R01 压力 | 有整数精确参考和条件反事实；一般分数策略不能升级资格 |
| G0-05 生命周期与应收 | `test_events_and_delayed_rights_match_independent_account`、`test_actual_corporate_account_matches_rights_before_and_after_each_session` | 有分红/拆股/终止及日程比较；仅声明事件与税额，不包含真实持有期税制 |
| G0-06 失败/缺历史与续持 | ADR 011 `evaluation_failures`、`test_outer_evaluation.py`；原生训练故障 worker | E18/E19 核验五类精确失败、首日/成功后/连续失败恢复、受限持仓和公司行为、合法零成交、全失败空预测、连续中止/not_run、系统/身份/会计错误中止及重哈希篡改拒绝；原生训练/候选审查仍中止。仅关闭本机合成外层诊断范围；实际临时故障事实不由回放认证 |
| G0-07 成本桥接/分母 | `test_cost_decomposition.py`、零分母绝对门槛、E12 | 显式 v2 逐笔 CNY/固定期初权益 bp/成交额 bp、逐日相对差和零分母已有手算及正式重算；B1/B2 精确自身状态分别 22/9 笔，旧 v1 默认保留；不代表成本已校准 |
| G0-08 阈值/资格/原 N02 案例 | `comparison.validate_acceptance`、E11/E13 | 当前核心重放原 N02 固定账户及原因果配方通过，整数模式最大权益差约 8.15e-10 元，数量/交收差 0；不是原训练重现，原阈值和负结果保留 |

## G1：生产映射与梯度

| 条目 | 实现与用例 | 证据判断和剩余工作 |
| --- | --- | --- |
| G1-01 因果风险/成本/状态 | `DecisionPolicy`、`test_native_gru_future_data_and_events_leave_prior_inputs_constraints_and_actions_unchanged` | 五类未来扰动在实际输入相关 GRU、非全现金账户与两账户核对第一步全部观察、约束、预测、配方、原始解及生产动作；可用后正对照必须影响后续预测；最终结果核对 E9 |
| G1-02 原始/生产动作与梯度边界 | [ADR 007 用例矩阵](ADR_007_DIFFERENTIABLE_PRODUCTION.md)、ADR 008 | 已逐项定位预算/零 slack、换手/漂移/锚点、非法数值/shape/dtype/device、额外约束、光滑导数与消融；当前完整用例核对 E9；meta 拒绝不是 GPU 实测 |
| G1-03 模型到跨日效用 | `test_gru_solver_mapping_multiday_utility_directional_gradient`、`test_allocator_to_multiday_utility_gradient_matches_directional_difference` | 有受控非零梯度和多步长；不能从这些内点用例推出所有边界导数唯一 |
| G1-04 非光滑机制 | 取整/deadzone 用例、`test_actual_fee_switch_and_zero_trade_discontinuity_keep_piecewise_gradients` | 最低费两光滑侧三步长、零交易费用跳跃、切换点单列；零换手有状态/原动作梯度证据。只覆盖声明分支，不推出所有活跃集交点导数唯一；核对 E9 |
| G1-05 失败不回退 | E8 的实际 worker 故障；非法输入/不可行优化拒绝 | 当前核心原生训练四类真实故障已验证中止、原异常/决策/原始解保留及后续 not_run；不产生成功输出，无自动零仓/等权 |
| G1-06 同状态/闭环反馈/映射消融 | 两账户相同首次完整动作审计；E11/E12 三路径、E14 当前四映射 | 当前身份已补严格原解/HiGHS/两 Tensor 的 12 解消融及四组闭环；strict_raw 拒绝保留，不伪称四种均可成交；B2 连续费用 268.0552 → 63.0552 元，精确费用均 36.0843 元，未见精确收益改善，资格仍 false |

## G2 与 C2/C3：训练和选择

| 条目 | 实现与用例 | 证据判断和剩余工作 |
| --- | --- | --- |
| G2-01/C2 时间截止与初始化 | `utility_fit.validate_feedback`；源未来/重叠/迟修订拒绝用例 | 有 cutoff、下一交易日与可见修订拒绝；只有声明的固定日频时点 |
| G2-02/C3 episode 和 detach | `episode.run_episode`；`test_episode_truncation_preserves_values_and_cuts_both_sides_of_return_ratio` | 有前向一致及伪分母梯度回归；没有中途恢复承诺 |
| G2-03 净效用和单位 | `net_utility_returns`，E1/E2，分数资金/费用手算 | 日净收益扣费一次、ddof=0 有证据；日内风险估计与完整账户损失不同，保持原配方声明 |
| G2-04 原连续验证选择 | `test_actual_utility_fit_freezes_encoder_selects_and_reloads_consistent_checkpoint`、E11 | 早轮最佳/平分规则/无梯度验证用例核对 E9；E11 再次观测连续 3/1/2、精确 1/2/3（2/3 平分）、tau-b=0，不改选模来源 |
| G2-05/C3 checkpoint 状态 | 同轮优化器/RNG、重载和冻结 encoder 用例；E11 候选完整核验 | 当前归档重算核验已选模型与对应候选状态一致；保存训练状态不等于支持断点续训 |
| C2-05 有限窗口 GRU | `DecisionPolicy.action` 每决策重新编码 | 有实现；不支持跨决策隐藏状态续接，新增须版本化 |
| C3 内存/预算 | max_episode_steps 硬上限、整 epoch 一次反传、E16 独立进程矩阵、E17 故障 | 当前冻结矩阵 24/24 完成、失败/超时/未运行 0，fit 9.454–43.782 秒，进程高水位 418205696–798928896 字节；12 组重复参数一致。仅一轮合成/单线程；128 资产长组截断 RSS 未更低，不保证随 K 有界内存、任意容量或并发可靠性 |
| R02/ADR 010 候选审查 | E2/E5/E6；完整候选、内层精确排名、预算、失败/中断及外层扰动 | 有实际反转与重算；E6 以真实零梯度平分触发早停，已完成候选可重算，后续 not_run 保留，删除已完成轮次被拒绝；软件范围不等于代理准确或市场有效 |

## G3 与 C1/C4：运行和归档

| 条目 | 实现与用例 | 证据判断和剩余工作 |
| --- | --- | --- |
| G3 因果性 | 五类原生 GRU 跨层扰动、模块扰动、外层执行扰动 | 当前两账户首决策的完整观察/约束/动作一致，信息可用后预测及反馈允许变化；原生用例结果核对 E9，不要求未来执行改变后整段动作不变 |
| C1 配置 | `utility_config.resolve`；未知键、任务、fold、seed、整数/生产/门槛/审查/外层版本拒绝 | E18/E21 核验严格解析及旧配置不补新字段，未知版本继续拒绝 |
| C4 共享 CLI/worker | `test_actual_worker_utility_training_and_independent_recomputation`；E18/E19/E21 | 当前真实训练、候选、外层恢复和 checkpoint 重算已有证据；训练状态/评价状态分开。Windows 个别进度事件追加遇锁转 stderr，不能保证 events.jsonl 包含全部进度；trial/candidate/归档仍持久化并核验 |
| C4 三条账户及明细 | `decision_evaluation.evaluate/compare`，E11/E12，生命周期差异用例 | 当前逐日权益/现金/股数/交收/应收/动作/费用及反馈、逐笔费用单位可重算，超限保留 |
| C4 归档与篡改 | 源/代码改变拒绝、辅助预测重哈希篡改、E5 候选重哈希篡改 | 有完整性与算术重算分离；不称其为训练算法重现或真实数据有效性 |
| G3/C5 旧路径无回归 | E21 当前预测/分数/目标/网站/worker 全套测试；E9 保留历史 | 当前完整主项目 584 passed，0 失败/错误/跳过；自动测试不能替代真实环境与市场研究 |
| G3 环境/C5-T6 | 锁定 Windows CPU 环境；E15 历史核心导出安装/逐候选同机重训；E22 内容核验；E23 当前普通安装/自包含重训 | E23 完成当前核心在独立环境中的普通安装、导入路径核验、自包含合成数据生成、源预训练、效用重训和候选/账户逐项比较；同机缓存依赖、Windows CPU、单折单 seed。Linux/GPU、冷机、非实现者复现和服务器部署仍未完成 |

## 当前关闭结论与下一步

R03 的原生故障/因果、成本 v2、原 N02 重放、四映射及 C3-R 资源矩阵按各自历史身份保留。当前 ADR 011 已补 C4-F 本机合成外层精确失败续持与独立回放，证据为 E18/E19/E20；E21 为当前完整回归 584 项；E23 补齐当前源码包的普通安装和自包含重训。R03-S 的本机软件交付条目可按 E18–E23 关闭，但独立范围仍限定为同机缓存 Windows CPU；Linux/GPU、冷机、非实现者复现、服务器部署和经济效用继续开放。进度事件追加遇锁的降级也留作运维改进项。分数代理资格保持 false，N04 主比较须先取得待研究配置的适用资格。

E16 同时暴露 64/128 资产组梯度约 `1e-13`–`1e-11`、更新很小；不改写资源成功状态，不将微小非零值称作有效学习。后续 R04-D 在开发期核对动作敏感度、活跃约束与数值尺度，再冻结研究设置；实际范围与测量口径见 [资源说明](../UTILITY_RESOURCE_AUDIT.md)。

N04 仍缺冻结同预算 B0/B1/B2、encoder 2×2、多滚动折/至少三种子、压力及日期块推断。N05 仍缺实际 Linux、授权公开样本与模型/数据卡、非实现者复现及服务器健康/恢复记录。本表不把任何这些要求替换成本机合成 smoke。

## R04-D 开发期梯度/动作诊断（不改变冻结核心）

诊断报告为 `artifacts/r04-d-diagnostic-20261023-001.json`，脚本为 `artifacts/diagnose_r04_d.py`。它固定历史资源矩阵中的源预测 checkpoint、账户协议、风险/成本 recipe、优化器和生产映射，仅在开发数据上重新运行 64/128 资产、3 步、full 截断场景，并对预测输出加入预声明的 `±1e-4`、`±1e-3` 十进制收益偏移作为动作敏感度探针。该报告标注为历史核心 `73cb717f...69c9e`，不是 ADR 011 当前核心的新训练或经济研究结果。

观察到：64 资产首日 61/64 个原始权重处于下界附近，128 资产为 125/128；预算使用约 28.3%，上界和预算均不活跃，生产修正量约 `1e-12`（128 首日约 `6e-10`）。预测绝对幅度约 `4.4e-4–6.4e-4`，而 `±1e-4` 偏移带来的首日原始权重 L1 变化仅约 `1e-10`（128 约 `8e-9`），`+1e-3` 偏移则出现约 `0.34/0.68` 的 L1 跳变并显著改变成本。这说明当前大资产池的训练信号同时受到大量下界/活跃集和求解器数值尺度影响；不能把 `1e-13`–`1e-11` 梯度解释为有效学习，也不能据此单独判断是模型、成本或求解器的唯一原因。

后续冻结研究前必须再做同一来源的方向向量扰动、相对/绝对误差分母规则、活跃集边界单列和 solver tolerance 对照；不得通过改成本、放宽约束或强迫交易制造梯度。R04-D 仍开放，N04 主比较未启动。
