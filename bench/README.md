# TemperQ Bench

产品名称为 TemperQ Bench；历史文档、协议字段和 `qwb` 模块标识保留兼容，不改写已保存的评价证据。

本目录保存上线后模型评价的协议、规则与研究说明；执行代码位于 `src/qwb/bench/`。CLI 与本地工作台“实验记录”均可发起评价、冻结协议和比较，公开网站不自动执行评分。新训练产物以 `runs/history/<run_id>/` 固定版本传入，`runs/<名称>/` 保留为最新兼容入口，详见 [研究闭环说明](../docs/RESEARCH_WORKFLOW.md)。

2026-10-22 衔接更新：Bench 结果记录训练运行、折预测版本和独立评价 ID；最终重拟合模型的身份另行保留。新协议 schema 5 保留 ADV 门槛，增加组合数据、指数名单和训练数据内容绑定；旧 schema 3/4 按原清单解释，不改写。评分实现改变后，比较需用当前评分器重评双方；旧文件仍可独立校验和追溯。

> **v0.4（2026-10-22）**：陈旧估值按停牌/退市记录归因、低波动风格对照、净超额分辨率、首批 7 对平台陷阱集（`qwb bench traps`）。见 [REAL_RUN_V0_4.md](REAL_RUN_V0_4.md)，协议 `protocols/cn_rank_5d_development_v4_lifecycle.yaml`。


已实现预测赛道的开发诊断闭环：已有预测 → 原快照重算标签与股票池 → 固定参考对照 → 日期块置信区间 → 独立现金/持仓回放与费用压力 → 短板诊断和中文报告。无需重新训练。

v0.3 新增：独立分数复权单位账户、逐笔费用/拒单、每日现金与实际保留数量、可实现等权和现金参考、净收益/回撤/年化超额区间、额外单边 10/25 bp 滑点、净账户版本配对比较，以及独立累计现金流/成交数量的对账。保留 v0.2 的信号、切片、毛标签收益空间和短板诊断。

本版报告区分“已发现历史信号”“假设条件下的研究净账户”和“已经具备实用证据”。没有通用 IC 分数可直接认证实盘；真实成交/容量、风险预算、复现、完整试验选择和前向确认仍待补齐。

当前可用：A 股日频 5 日/20 日排序任务的评分代码、固定参考排名、预测结果之间的配对比较。当前机器已跑通真实 5 日任务；20 日任务使用合成案例核对标签契约，尚未生成真实训练锚点。

未实现：正式经济评分、整数股数与公司行动逐笔账本、直接持仓/RL 策略回放、平台完整训练与搜索预算运行器、社区百分位、随机面板分布和全部挑战案例。当前结果比较不能代替平台完整工作流主分。

## 快速运行

在仓库根目录的 PowerShell 中运行。先用当前评分器建立新协议，固定本地行情、费用/账户规则和 LightGBM 参考产物；以下输出路径必须不存在。

```powershell
# 评估自己训练好的运行目录；输出目录必须不存在。
.venv\Scripts\python.exe -m qwb.cli bench init runs/cn_rank_5d_lgbm --output bench/protocols/my_current.yaml
.venv\Scripts\python.exe -m qwb.cli bench model runs/cn_rank_5d_lgbm --protocol bench/protocols/my_current.yaml --output runs/bench/anchor_current
.venv\Scripts\python.exe -m qwb.cli bench model runs/my_new_model --protocol bench/protocols/my_current.yaml --output runs/bench/my_model_current

# 对比两次同协议、同评分器、证据与覆盖合格的 Bench 结果。
.venv\Scripts\python.exe -m qwb.cli bench compare runs/bench/anchor_current runs/bench/my_model_current --output runs/bench/comparison_current

# 检查本地评分文件、原始输入，重算逐日 IC 均值和分数。
.venv\Scripts\python.exe -m qwb.cli bench verify runs/bench/my_model_current --inputs

# 旧协议不被当前版本接受时，只检查保存文件的哈希与绑定关系。
.venv\Scripts\python.exe -m qwb.cli bench verify runs/bench/cn_rank_5d_v0_3_replay --integrity-only
```

历史真实示例见 `runs/bench/cn_rank_5d_v0_3_verified/report.md`。它以参考模型自身为候选，用于验证闭环，不表示平台改进。旧版及早期探索目录保留；升级后须为双方新建同版协议并重新评分。

安装了项目后也可使用 `qwb bench ...`，或在本地工作台“实验记录”中发起评价和比较。

## 在其他机器或任务上建立协议

```powershell
.venv\Scripts\python.exe -m qwb.cli bench init runs/cn_rank_5d_lgbm --data-dir <你的数据目录> --output bench/protocols/my_development.yaml

# 示例：预先规定正信号至少 1 IC 分，并要求相对锚点至少提高 0.5 IC 分。
# 只是任务开发目标示例，不是学术标准或实盘门槛。
.venv\Scripts\python.exe -m qwb.cli bench init runs/cn_rank_5d_lgbm --output bench/protocols/my_targets.yaml --min-signal-score 1 --min-anchor-gain-score 0.5
```

`init` 固定参考目录中的 `task.yaml`、`config.resolved.yaml`、`folds.json`、`predictions.parquet`，以及存在时的 `metrics.json`、`data_manifest.json`；行情分区、快照元信息、ST、指数名单、组合数据配方和来源均使用完整 SHA-256。新训练的数据清单必须与协议实际输入匹配。参考应当是平台选定的 LightGBM 运行；该命令只读取产物，不认证这些文件确实来自 LightGBM 训练。请保留原目录，不要在其中重训覆盖。

schema 5 支持组合数据以及 all / st / all_ex_st / list / csi300 / csi500 / csi1000 股票池；使用训练相同的数据覆盖顺序和历史名单规则。原始名单缺失时明确失败；覆盖行情的行若缺交易状态，不能从基础行继承“正常”。旧训练缺少数据内容清单时会记录证据局限。

协议始终为 `development`。默认块长 20 日、10,000 次重采样、最少 60 个有效日期；同时要求不少于四个块长度的有效日期才能给区间。敏感性检查默认增加 10/40 日块长。这些是可运行的开发参数，尚未完成正式统计校准。可以在新建协议时指定 `--block-days`、`--resamples`、`--min-days`，参数变化后必须在新协议下重评双方。`--min-signal-score` 与 `--min-anchor-gain-score` 比较区间下界，而不是只比较均值。

协议内路径是当前机器的绝对路径，原始行情不分发。行情搬家后可以在 `model` 命令增加 `--data-dir`，内容仍必须逐文件匹配；参考目录搬家需新建协议并重评。

## 输入与输出

推荐输入 q-w 已有运行目录：

```text
predictions.parquet
task.yaml
config.resolved.yaml
folds.json
metrics.json               # 可选；只读取声明检查与台账信息，不复用绩效数值
```

也可以直接传入预测 parquet/CSV，必需列为 `code`、`decision_time`、`task`、`prediction`、`model_version`、`data_snapshot_id`。时间必须明确带时区并与本地 15:30 决策时间吻合；同一标的/时点不得重复。未知股票、日期、任务、快照和无穷预测会使评估无效。缺失预测保留为缺失。

当前只检查现有按年滚动折声明，运行目录还须包含任务和解析配置。仅有预测文件时仍能算诊断分与参考表，但训练来源为 unknown，不给排名或样本外通过结论。折声明检查不核验权重、外部特征或训练程序，不能证明没有泄漏。

```text
scorecard.json             # 结构化状态、分数、排名、区间、覆盖、来源和哈希
daily_metrics.parquet     # 原交易日期网格，逐日 IC、有效股票数、覆盖和排除状态
diagnostic_paths.parquet  # 前排股票毛标签收益、五分位收益、流动性组 IC
diagnostics.json          # 切片、块长敏感性、假设成本空间和数据缺口
readiness.json            # 当前适用阶段、各证据门槛、短板与下一步
protocol.json             # 本次实际使用的完整协议
economics.json            # 条件研究账户的净绩效、区间、费用、数据资格与假设
account_paths.parquet     # 每日现金、持仓市值、净值、净收益、费用和换手
fills.parquet             # 决策日、执行日、数量、原始开盘价、费用、拒单/缩量原因
holdings.parquet          # 每日实际保留单位、过去可得估值、市值和陈旧标记
environment.json          # Python、系统与相关依赖版本
report.md                 # 与 scorecard 同源的中文报告
checksums.json             # 评分产物及 scorecard 的完整哈希清单
```

结构错误或快照/参考变化时，生成 `INVALID` 报告，不生成成绩。输出目录不覆盖；已有目录需要换新名字。CLI 对执行无效返回 1，对正常完成评分返回 0；模型信号 FAIL 或 INCONCLUSIVE 本身不是程序运行错误。

## 如何理解结果

- IC 分 = 100 × 每日 Rank IC 的等权均值。IC 0.05 对应 5 分，分数可以为负。
- 股票池来自协议，标签由原行情重算；候选缺预测不会改变参考样本。未成熟标签、预热不足及少于 20 只股票的日期预先排除。
- 覆盖必须完整，所有应评分日期的 IC 均可定义，候选与参考训练折声明合格，才给开发参考排名。常数预测没有有效 IC，不会被算作零能力的合格成绩。
- 参考为 5 日反转、20 日动量、20 日低波和固定 LightGBM；加入候选共 5 个。均值保留 8 位小数判断并列。排名是点估计，不代表显著优于其他模型。
- 用同一组日期块重采样候选、参考和差值，保留原日历上的缺失位置。正信号门槛默认 0：区间下界大于 0 为 PASS，上界不大于 0 为 FAIL，其他为 INCONCLUSIVE。日期不足则不给区间。
- 比较命令计算新减旧；相同结果分差为零，严格优于目标的结论不通过不等于训练失败。不同协议、修改过的评分产物、不完整覆盖或不同评分器实现拒绝比较。
- 四个固定参考使用 Bonferroni 百分位区间近似控制同组多重比较。它不修正全部模型搜索、用户调参、反复看测试和全部探索性切片；没有完整试验矩阵时不输出 DSR/PBO。
- 成本空间是前 10% 股票的重叠标签事件分析，单位 bp/标签期；假设额外完整买卖摩擦为 10/30/60 bp。没有成交或真实持仓，不能把残差当作净收益或年化成策略业绩。
- `readiness` 仅给出修复输入、继续研究、已有历史信号可进入组合与成交验证研究三个阶段。当前不会给出仿真准入或实盘准入认证；所有短板记录都注明观测、门槛和下一步，不将症状冒充已证实的原因。
- `bench verify` 独立从成交累计现金流与数量，核对每天持仓、净值、费用和换手，重算净指标和区间。`--inputs` 还检查原始输入哈希。它不重新生成订单、查证成交或加载原模型；本地一致性不是第三方防作弊认证。
- `--integrity-only` 用于保留的旧报告，只检查清单、文件哈希、协议哈希和已有训练绑定；不接受协议、不重算算术，也不检查原始输入。不能与 `--inputs` 同用；旧协议的算术复核需要原评分环境。比较入口始终执行完整验证。
- IC 通过不代表扣成本盈利；同锚点持平不构成训练失败。当前区间只描述日期不确定性，没有完整重复训练和模型选择的不确定性。
- 所有当前结果均为本地历史开发证据，不作前向验证或反作弊认证；正式分数资格恒为 false。

后续范围与研究依据见 [SPEC.md](SPEC.md)、[实用证据与近期文献](references/PRACTICAL_EVIDENCE_REVIEW.md) 和 [FinRL/端到端研究记录](references/FINRL_E2E_REVIEW.md)。

## 研究账户规则与边界

协议 schema 3 固定 `weight_research_v1`。每 h 个交易日收盘决策，次日开盘执行；经济窗口最后一个收盘估值，不因未来标签未成熟缩短窗口。共同股票池仅要求决策日股票池和三个规则参考的历史输入完整，不使用未来标签；排除数量进入报告。候选或锚点在共同股票池缺预测时，账户不评估，保留信号诊断。

排序账户固定前 10% 等权，参考为四个信号参考、同规则可实现等权及现金。先卖后买，受阻持仓保留单位数量，新买按剩余现金同比缩量。佣金默认单边 3 bp（含经手等费用的研究假设），加上 STRUCT 上月输入半价差与日期生效的印花税。不对缺失价差估计填零；缺开盘/复权因子、涨跌停价、状态或价差时拒单并留痕。额外单边 10/25 bp 分别作用于候选及等权的实际成交。

研究经济量 `252 * 10000 * mean(候选日净收益 - 可实现等权日净收益)`，单位年化 bp，100 bp 为 1 个百分点。CAGR、回撤、年化波动、零无风险利率 Sharpe（ddof=1）单列；零波动 Sharpe 为 null。区间是日期块边际区间，不校正完整模型搜索或执行不确定性。`compare` 同时展示新减旧日净收益的配对年化增量。

账户单位为 `原始股数 / adj_factor`；恒定单位乘复权价格近似含公司行动的总回报再投资。它不是实际股数账户，未模拟整数手、最低佣金、现金分红事件、冲击、容量、开盘队列、延迟或退市清算。当日状态/开盘价只是日线代理。缺持仓估值沿用最近过去复权价并标记；候选或等权存在任一次陈旧估值/关键价格费用缺口时，对比标 `DIAGNOSTIC_ONLY`，不能获得净账户通过结论。即使 `CONDITIONAL_RESEARCH` 也只有在上述假设下的含义。

期末持仓不强制平仓，仍计市值，不收假想退出成本，因此期末净值不是可提取现金。`net_account` 的 PASS 仅表示输入合格、无上述缺口且研究净超额区间下界严格大于零；不能抵消容量、风险、复现、搜索和前向证据的缺失。正式经济分、仿真准入和实盘准入保持 false。

## 目标持仓扩展

直接持仓提交使用独立 `qwb bench targets` 和 `target-verify`，原始股数、真实现金、T+1、未成交与逐日权益回放。示例、纯 JSON 契约和限制见 [目标持仓 Bench](../docs/TARGET_BENCH.md)。当前仍为诊断参考，不支持公司行为、已有跨日挂单或容量认证；不把权重塞入旧预测分数入口。
