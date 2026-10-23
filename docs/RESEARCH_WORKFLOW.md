# 可追溯的研究闭环

更新：2026-10-22。当前可用流程是“本地训练 -> 固定版本 -> 独立 Bench -> 配对比较 -> 导出研究摘要 -> 手动发布至 HF / 网站”。公开网站不执行模型，也不认证提交的摘要。

## 能力矩阵

| 能力 | 当前状态 |
| --- | --- |
| LightGBM / Ridge 日频年滚动训练 | 可用；原生标签限 return/return_rank，输出 score |
| 历史内层选轮数、特征消融 | LightGBM 可选清除标签重叠的尾部验证；`train.features` 可显式指定子集 |
| 每次训练独立归档、失败/取消记录、产物哈希校验 | 可用；本地文件记录，不能证明没有人为删除 |
| CLI / 网页统一训练执行入口 | 可用；共用持久化 JobSpec 与 worker |
| 网页后台训练、冻结协议、Bench、比较 | 独立进程执行，可关闭网页和 UI 进程 |
| 中断检测与重试 | 可用；重试新建训练 run，重新计算，尚无折级训练恢复 |
| 数据下载和外部模型体检 | 保持原有 UI 线程执行；下载缓存可复用，尚未迁移到独立 worker |
| Bench 快照与组合数据任务 | A 股日频 5/20 日；schema 5 绑定实际数据内容及 ADV 门槛 |
| 指数股票池、自选列表的独立 Bench | schema 5 支持 all / st / all_ex_st / list / csi300 / csi500 / csi1000；使用训练相同的池和来源覆盖顺序 |
| 任意模型、自定义月/周滚动 | 外部标准预测可诊断；原生训练与来源验证仍有边界，见专业审查 |
| 历史折预测与最终重拟合模型区分 | 可用；分别记录版本，Bench 评价历史折预测 |
| 本地实验页、评价关联、信号配对判断和交易诊断差值 | 可用 |
| 研究摘要 ZIP 与 HF 模型卡元信息 | 可用；仅摘要，无权重、行情、执行代码或完整复现材料 |
| 网站自动训练、自动认证、公开跨协议收益榜 | 不提供 |
| MLflow、Optuna、Qlib 模型适配、Agent 自动研究 | 待实施；未加入默认依赖 |
| 多年真实市场增量收益证明、冻结后前向确认 | 未完成；合成演示不作为相应证据 |

## 网页工作流

运行 `start.bat` 或 `qwb ui`，打开启动地址。

1. 在训练页选择数据和模型，提交任务。训练页和 CLI 均保存序列化配置；同名训练不会覆盖归档版本。
2. 在“实验记录”打开具体版本，查看运行、配置、数据、折预测和最终模型的身份。
3. 对支持的快照或组合数据任务，选择一个 LightGBM 版本作为固定参考，使用“以此模型冻结新协议”。冻结前设置日期块长度、重采样次数和最少日期；不要根据结果反复调整这些设置。
4. 为候选版本选择同一协议，运行独立 Bench。评价从协议绑定的实际数据重算标签与股票池，核对训练数据清单，结果可反查对应训练。
5. 在“比较 Bench 结果”选择旧、新评价。系统核验文件、协议、评分实现、日期、样本和参考路径；不匹配时拒绝直接比较。
6. 在评价页面导出含 Bench 的研究摘要。将拟公开的文件放入自己的 HF 模型仓库，提交后在网站分享该仓库；发布是用户的显式操作。

摘要 ZIP 包含 `README.md`、`evidence.json`、`checksums.json`。摘要型仓库不包含可加载模型，README 会说明这一点。若要分发权重，仍须使用现有模型包流程，并核对权重、代码与数据许可。网站读取固定提交的 README 元信息；它不下载摘要 JSON 并独立重算，不会凭这些字段升级为“平台验证”。

## CLI 工作流

```powershell
qwb run examples/run_cn_rank_5d.yaml
qwb runs list
qwb runs verify RUN_ID

# 固定参考应为 LightGBM；使用固定归档目录
qwb bench init runs/history/ANCHOR_RUN_ID --output bench/protocols/my_development.yaml
qwb bench model runs/history/OLD_RUN_ID --protocol bench/protocols/my_development.yaml --output runs/bench/old_001
qwb bench model runs/history/NEW_RUN_ID --protocol bench/protocols/my_development.yaml --output runs/bench/new_001
qwb bench compare runs/bench/old_001 runs/bench/new_001 --output runs/comparisons/compare_001
qwb bench verify runs/bench/new_001 --inputs

qwb runs export NEW_RUN_ID --evaluation runs/bench/new_001 --output artifacts/evidence/new_001.zip
```

大写 ID 为占位符，请替换为 `qwb runs list` 输出。Bench 输出和导出 ZIP 必须使用新路径，不会覆盖已有结果。

CLI `bench model` 将评价位置登记到当前工作目录的 `runs/evaluations/`，因此应在项目工作目录执行，网页才能找到它。库函数 `evaluate_model` 只有显式传入 `workdir` 时才登记。

旧结果可复制纳入归档，原目录不改写：

```powershell
qwb runs import runs/old_model
```

导入记录标为 `legacy_import`；导入时间不冒充训练时间，缺失的代码哈希、折预测版本和旧运行信息不补造。同名训练首次替换无版本标识的旧结果前，也会先导入历史快照。显式重复导入旧目录会产生新的导入记录。

## 后台任务与中断

网页训练、冻结协议、Bench 和比较通过 `python -m qwb.worker` 执行。一个工作目录内的这些作业依次执行，避免同时抢占内存。CLI 训练使用同一个 worker 入口，但默认前台等待，关闭 CLI 进程会中断该次执行。

```powershell
qwb jobs list
qwb jobs status JOB_ID
qwb jobs cancel JOB_ID
qwb jobs recover JOB_ID
qwb jobs retry JOB_ID
qwb jobs start JOB_ID
```

`recover` 只在执行锁已释放时将残留 running 状态标为失败，不会中断仍在运行的 worker。打开任务页也会检查这一状态。已失败或取消的序列化任务可在网页重新运行；训练重试新建 run，旧产物保留。训练取消在日志/折边界生效；费用账户长阶段暂不保证即时取消。

后台任务依赖当前 Python 环境、工作目录和路径仍可用。系统重启后没有常驻调度守护进程；queued 任务可用 `jobs start` 启动，残留 running 任务先 recover 再 retry。当前不是多机队列，也未提供单任务资源配额或超时强制终止。

## 文件与身份

```text
runs/
  <名称>/                    最新结果的兼容副本
  history/<run_id>/          每次尝试的归档，成功后不再由程序改写
    run.json                状态、时间、来源、身份与产物清单
    task.yaml / config.resolved.yaml / folds.json
    data_manifest.json      实际输入路径与 SHA-256，训练开始和结束时复核
    predictions.parquet / metrics.json / report.md
    models/ / final_model/
  jobs/<job_id>/            配置、状态、日志、worker.log、产物位置
  evaluations/<id>.json     评价位置和 scorecard 哈希索引
  bench/<evaluation_id>/   网页生成的评价目录
  comparisons/<id>/        网页生成的比较目录
```

- `experiment_id` 按完整任务卡内容归集；任务卡改变后进入不同实验。
- `run_id` 标识一次尝试；相同配置也不会复用。
- `prediction_version` 由历史折模型文件、配置、快照声明及实际数据内容哈希生成，写入预测表的 `model_version` 列。
- 归档顶层的 `model_version` 指向供后续推理的最终重拟合模型，不能用历史折评分直接证明它的未来表现。
- `evaluation_id` 标识一次 Bench；`protocol_id` 采用完整协议哈希。Bench 冻结 `training-run.json`，不回写训练归档。
- `code_hash` 覆盖 qwb Python 源码与 YAML 规则；环境在 metrics 中另记。`data_content_hash` 另行绑定实际输入文件，不能证明供应商时点、修订历史或数据授权；依赖与第三方源码仍非完整环境快照。

兼容入口使用副本，便于 Windows 和旧脚本使用，会增加磁盘占用。同名重跑后，`runs/<名称>` 更新；Bench 校验有版本记录的最新副本后使用对应归档。长期证据应引用 `history/<run_id>`。

应用只保证自身不覆盖成功归档，普通文件权限不能阻止手动编辑或删除。哈希用于发现变化，不是第三方认证；磁盘损坏等仍需外部备份。接受门槛锁仍按原输出名称保留，不能证明用户未换名称或在别处调整门槛。

## 评价解释

比较分别展示配对 Rank IC 的改善、变差、差为零、未达预设增量或证据不足。严格改善要求区间下界超过预设门槛；同版本与自身比较不会被称为退化。

净账户差继续报告区间，不作为统一“新版更好”的标签。佣金、税、滑点、换手、拒单与数据异常提供新减旧的诊断差值，不附加未计算的显著性声明。

新协议采用 schema 5，在 schema 4 的 ADV 门槛基础上绑定组合数据配方、来源、指数名单及训练数据清单。历史 schema 3/4 按各自原清单解释，不给旧协议补造新证明。当前接受的协议可以做文件与算术校验；更早的账户假设或协议需用原评分环境复核算术，也可用 `qwb bench verify OLD_RESULT --integrity-only` 仅检查已存文件哈希和绑定关系。这个模式不校验原始输入、不重算成绩、不授予比较资格，也不是第三方认证。当前版本比较要求用当前评分器重评双方。完整协议仍含机器路径，跨机器须按 Bench 文档校验数据并建立新协议。

训练报告使用相同股票/日期样本上的配对 IC 差和三个固定参考的 Bonferroni 校正 Newey-West 正态近似区间；缺失日期不拼接。自然语言 acceptance 只是用户声明，不会被自动解析执行。该信号筛查、训练报告简化费用诊断和独立 Bench 账户分别解释，不作为统一投资准入结论。

2024–2025 已用于开发，不能作独立确认期。北交所异常、退市估值与陷阱集边界见 `bench/REAL_RUN_V0_4.md`。本轮 2018–2023 开发子集的训练对照、文献依据、配置及专业用户边界见 [专业研究链路审查](PROFESSIONAL_RESEARCH_AUDIT_20261022.md)。数据缺口和独立前向证据仍待补齐。

## 自检演示

```powershell
.venv\Scripts\python.exe scripts/run_research_demo.py --output artifacts/my-demo
```

目标目录必须不存在。脚本在隔离目录生成 420 日 × 40 只股票的合成数据，通过实际入口完成两个模型、固定协议、独立账户、比较和摘要 ZIP，仅验证链路。

浏览器验收需要 `website` 的 npm 依赖及 Chrome。先在演示目录运行 `qwb ui --no-browser --port 8766`，再在主目录执行：

```powershell
$env:QWB_QA_WORKDIR = 'D:\path\to\my-demo'
node scripts/qa_workbench.mjs
```

脚本只向标记为合成数据的目录提交评价/比较，截图与下载文件写入忽略的 `artifacts/workbench-qa/`。
