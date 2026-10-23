# 目标持仓独立回放

本入口接收目标权重，按实际原始股数/现金回放。它与旧的五日分数 Bench 分开，不能用权重伪装成预测。v1 保持无公司行为语义；新增 v2 支持显式分红、应收、整数拆股与终止现金索赔。现有跨日挂单、未知公司行为和非整数拆股仍会拒绝。设计见 [ADR 003](specs/ADR_003_TARGET_BENCH.md)、[ADR 004](specs/ADR_004_ACCOUNT_LIFECYCLE.md)。

## 合成示例

从仓库根目录，在已安装主项目的环境中运行，不要求 PyTorch 或求解器：

```powershell
.venv/Scripts/python.exe -m qwb.cli bench target-fixture --output artifacts/target-fixture
.venv/Scripts/python.exe -m qwb.cli bench targets artifacts/target-fixture/submission.json --protocol artifacts/target-fixture/protocol.json --output artifacts/target-result
.venv/Scripts/python.exe -m qwb.cli bench target-verify artifacts/target-result --inputs
```

输出目录必须新建，不能覆盖。示例仅有三次日频决策、初始现金 10000、固定策略；第一日买入 900 股、费用 5、余款 995，收盘权益 10895。次日不会将偏离目标的九股直接成交，因为示例显式指定卖出单位 100。净权益会随真实持有数量和价格漂移变化。

带生命周期的新版本示例（同样只需主项目依赖）：

```powershell
.venv/Scripts/python.exe -m qwb.cli bench target-fixture --lifecycle --output artifacts/target-lifecycle-fixture
.venv/Scripts/python.exe -m qwb.cli bench targets artifacts/target-lifecycle-fixture/submission.json --protocol artifacts/target-lifecycle-fixture/protocol.json --output artifacts/target-lifecycle-result
.venv/Scripts/python.exe -m qwb.cli bench target-verify artifacts/target-lifecycle-result --inputs
```

该例第一日登记 900 股，每股分红 1；次日先 1 拆 2，再按拆后每股 0.5 除权。应收 900 计入权益，但不用于买入；当日新增买入 100 股，收盘有 1900 股、现金 490、应收 900、净权益 11270。第三日分红支付，最终净权益 11444.74。示例行情完全合成，不表示市场收益或策略能力。

## 协议与提交

`target_protocol_v1` 是纯 JSON，声明任务 `cn_alloc_daily_v1`、`initial_state`、`fees`、`constraints`、`sessions`、`corporate_actions`、`data_kind`、`data_license` 与 `execution_model`。CLI 示例生成可审查的完整文件；所有字段必须明确，未知字段报错。

每个 session 声明决策身份、15:30 决策、下一声明交易日 09:30 开盘/15:00 收盘、再下一开盘、决策时可选资产和实际执行行情。连续 session 的执行日历必须衔接；协议的交易日/状态/报价仍由数据提供者声明，没有交易所数据认证。执行数据不会用于确定决策候选池。

行情记录包含 `asset_id/open/close/upper_limit/lower_limit/suspended/buy_lot/sell_lot/max_quantity`。缺失行情显式用 null；涨跌停、停牌、资金、费用、整数交易单位和容量代理均可阻止成交。买入只允许决策候选资产；已有持仓不在候选池也保留估值和卖出机会，不能因无新信号消失。部分卖出按 `sell_lot`，整仓清空允许剩余零股；具体板块及券商交易单位必须另核，不能把这个简化开盘模型称为交易所撮合。

v1 的 `corporate_actions` 继续必须为 `[]`。空事件是协议方声明，不是平台证明市场中没有分红、拆股等事件；市场数据结果始终 `DIAGNOSTIC_ONLY`，不能用后复权价格代替原始股份账本。

v2 使用 `target_protocol_v2/account_state_v2/target_submission_v2/target_weights_v2`，指定 `raw_share_lifecycle_v2`。状态新增 `entitlements/processed_events`；目标新增 `receivable_weight`，等于决策时实际应收占比，股票、现金和应收意向占比合计为 1。v1 目标不能用于 v2 账户。示例生成完整 JSON；事件字段、同刻顺序、税额与原始股数单位以 ADR 004 为准。

日频事件在声明交易日 09:00 处理，收盘 15:00 登记。先确认登记权利，除权后才将其计入权益，到账再转现金；分红扣税只接受明确的税收豁免或声明金额，不声称重建真实持有期税制。拆股同步调整可售和未交收批次；会产生残股时拒绝。终止估值形成索赔权，不自动卖出；终止后禁止重新入池，执行价格应为 null。协议未声明的退市处置不会由系统推断。

`target_submission_v1` 包含冻结 `protocol_id` 与 `decisions`。每个声明决策恰有一项 `target_weights_v1`，绑定实际 `state_id`、时点、资产池、快照、任务、模型版本和执行时间；股票加现金为 1，有限、非负，满足协议仓位/最低现金限制。候选资产与已有持仓的并集由账户生成，不以未来是否有行情筛选。

训练或求解失败可以提交 `decision_failure_v1`：明确 `decision_id/decision_time/task/model_version/data_snapshot_id/state_id/universe_id/protocol_id/reason`。它取消新增动作并保留已有仓位，继续按市场估值；冻结持仓违反风险限制时，报告实际违约。缺失决策不能自动转换为失败或现金策略。

固定目标提交必须针对真实回放路径逐步生成，不能拿上一目标权重替代状态。专业用户可本地调用 `qwb.bench.targets.replay(protocol, policy)`；policy 接收不可变 `DecisionContext`，含实际状态、决策资产与限制，返回目标或显式失败。此为受信任本地扩展，不是远程执行环境，平台不认证用户策略代码的因果性。公开网站和 CLI 都不导入提交代码。

v2 上下文另有 `known_actions`，仅包含决策时已经可用的不可变事件。v2 本地策略返回 `qwb.portfolio.lifecycle.LifecycleTargetWeights`；`receivable_weight = context.state.receivable_equity / context.state.equity`。执行期间新发生的除权/支付可能使原意向无法达到，资金限制与目标/实际差异如实保留，不自动修复目标。

## 产物与审查

结果包含冻结的协议/提交原文、输入内容身份、`scorecard.json`、`replay.json`、可读报告与 `checksums.json`。逐日实际状态、T+1、费用、成交/未成交、目标/实际权重、风险违约、隔夜/日内损益和自融资残差均保留。

v2 另保存逐事件现金、证券市值、应收与损益、声明扣税、状态身份及会计残差。`total_cost` 是交易费用，`declared_withholding` 单列且已经计入净权益，不重复扣除。登记、拆股和支付不应凭空产生收益；逐日净权益包含事件损益与交易费用，可由冻结输入完整重放。

`target-verify` 先验证文件清单和哈希，再用记录的实现重放，核对完整账户路径与汇总。`--inputs` 还重哈希原始文件；未加时只使用结果内的冻结输入，可离线复查。源码实现改变时需要原评分实现才能作精确重算，不把旧结果改写成当前版本的验证证据。哈希和重算不构成外部认证。

无效提交会保存 `INVALID` 和错误，返回非零，不生成成功账户。冻结日内预测现可通过 [共享组合层](PREDICTION_ALLOCATION.md) 生成目标，并在共同账户协议下回放 B0/B1；完整冻结研究比较、网页任务队列、连续效用训练及模型上线仍待完成。后续门槛与边界按 [主 SPEC](specs/E2E_RESEARCH_PLATFORM_SPEC.md) 推进。
