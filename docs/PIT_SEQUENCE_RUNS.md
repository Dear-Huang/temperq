# 原始行情序列训练

本入口实现 SPEC 的预测分支：原始行情 → 因果窗口 → GRU 或 Ridge → 月/周滚动预测。它通过现有 CLI、worker、运行归档和摘要导出，仍属于历史开发研究。冻结预测可进一步走 [共享组合层](PREDICTION_ALLOCATION.md) 和独立目标 Bench；连续效用训练尚未实现。决策依据见 [ADR 002](specs/ADR_002_SEQUENCE_RUNS.md)。

## 复现软件流程

在 [研究环境](E2E_IMPLEMENTATION.md) 安装完成后，从仓库根目录运行：

```powershell
artifacts/environments/e2e/Scripts/python.exe -m qwb.cli e2e fixture --output artifacts/sequence-fixture
artifacts/environments/e2e/Scripts/python.exe -m qwb.cli run examples/run_e2e_prediction.yaml --check
artifacts/environments/e2e/Scripts/python.exe -m qwb.cli run examples/run_e2e_prediction.yaml
```

`--check` 校验配置、任务语义和展开参数；输入文件内容及覆盖在运行时校验。第一次命令要求新目录。生成器产出 CC0 声明的随机数值样本与简短 smoke 配置 `artifacts/sequence-fixture/run.yaml`；示例配置则使用 60 日窗口和三个种子。示例日历含普通工作日，仅适用于该合成样本，不是中国交易所日历。

固定三种子、60 日窗口的 B0/B1 流程对照：

```powershell
artifacts/environments/e2e/Scripts/python.exe scripts/run_sequence_demo.py --output artifacts/sequence-demo-new
```

两种模型使用相同数据、观察、标签、样本、折和每次拟合时间上限；参数容量与实际计算量不同，不能称为严格等算力比较。生成前冻结配置，双方结果均保留，不按结果择优。输出 `plan.json`、`comparison.json` 及两个完整运行。Ridge 是确定性基线，三个种子是重复控制，不作为三个独立统计样本。

## 接入自己的数据

`data.source: pit_bundle`，`data.dir` 相对配置文件所在目录解析。四个必需输入文件共同构成内容身份：

| 文件 | 内容与约束 |
| --- | --- |
| `manifest.json` | `schema_version: pit_numeric_v1`、`data_snapshot_id`、`data_kind`、`license`、`universe_policy: explicit_daily_declarations`，字段严格校验 |
| `calendar.json` | `{"sessions": ["2023-01-03", "2023-01-04"]}`，完整、有序、无重复的当地交易日期；不能从缺失行情推断休市 |
| `bars.parquet` | `code/date/open/high/low/close/volume/amount/available_time`，可选非负整数 `revision`；同资产/日期/可用时间/修订不得重复 |
| `universe.parquet` | 恰好为 `code/date/available_time/eligible/revision`，`eligible` 必须布尔值；每个决策日有明确可见的日声明，未声明资产不进入候选池 |

`data_kind` 只能为 `synthetic` 或 `market_development`。字段存在不意味着来源已获独立时点认证；市场结果固定 `DIAGNOSTIC_ONLY`。`license` 是数据提供方的授权声明，不由平台代为认证。

所有可用时间带明确时区；日期是不带时区的当地交易日。该配方读取完整日线，`available_time` 不得早于当日 15:00；真实供应商晚到、重发、修订必须保留实际时间，不能统一回填收盘。价格为可追溯原始价格，成交量和成交额分别采用股、元；缺失用空值，不前向填充。复权、公司行为与退市覆盖不完整时，结果仍有数据风险。

每个决策窗口仅选当时可见的最新修订。至少满足 `min_history` 个完整价格日，并且决策日价格可见，才生成该资产预测。未入池、短历史和缺失末日价格分别体现在逐日覆盖数量中；不会按未来标签好坏筛选预测。此资格规则只约束分数预测，不能用于丢弃账户中的已有持仓。

标签从**下一交易日**的 `close/open - 1` 构造。训练、历史验证分别在各自截止时点取可见修订；最终展示标签另在预声明的 `evaluation_asof` 取值。未知标签保留空值并报告覆盖，不把未知收益填成零。该标签不包含旧持仓隔夜、成交失败或成本，不能直接作为组合净收益。

## 训练与产物

模型为 `sequence_gru_v1` 或 `ridge_factor_v1`。GRU 不要求因子输入，接受窗口数值和逐通道掩码，标签用训练段均值/标准差进行尺度变换以稳定优化，预测与历史验证 MSE 还原为原始收益单位，再按验证 MSE 早停；Ridge 使用同一窗口生成的 r1/r5/r20、波动、成交活动与缺失指示，特征标准化只拟合训练样本。Ridge 的正则约定复用原生基线：`alpha × n_train × 1e-4`，实际值写入拟合记录。输入和模型同时变化，B0/B1 不能隔离纯表示增益。

`folds.frequency` 为 `M` 或 `W-FRI`；首末测试日必须是日历中的交易日。`train_days: null` 为扩展训练，整数则为滑窗。标签按实际可用时间 purge，`embargo` 是明确时间时长，默认 0 秒；不是按股票行数截断。训练有效日期不足时失败。验证只选 GRU epoch，不重新拟合到验证段；没有最终前向重拟合模型。

`train.max_samples` 限制物化样本数，超限失败，不静默抽样；大规模流式数据暂未实现。`max_seconds` 是每折/每种子的拟合上限，`cpu_threads` 控制 GRU 的 PyTorch 线程，Ridge 使用 NumPy 本机 BLAS 配置。当前 GRU 为 CPU 实现，不因机器有 GPU 就宣称使用了 GPU。

每次尝试保存到 `runs/history/<run_id>/`，成功后更新配置的输出目录。失败、取消不覆盖上一成功版本。除既有产物外，保存：

- `observation_manifest.json`：数据、加载内容、日历与变换配方身份。
- `coverage.parquet`：逐日声明、资格、缺失/短历史覆盖。
- `trials.json`：运行前冻结的折与种子，成功、失败、取消、未运行状态。
- `models/fold-*/`：逐折 checkpoint；保存后实际重载并逐值复现测试预测。
- `seed_predictions.parquet`：各种子预测；`predictions.parquet` 为等权平均。
- `folds.json`：截止时点、标签成熟上界、样本数、purge、选中 epoch 与拟合记录。

`model_version` 留空，`prediction_version` 标识这组历史折模型，不能冒充未来上线模型。实验页可查看报告、归档与导出白名单摘要。旧 5/20 日 Bench 不支持这个新任务；不能通过改名或将目标权重伪装成分数来绕过。

Rank IC 沿用至少 20 只股票/日的证据门槛；不足时为空并报告日期覆盖。MSE 和 IC 仅为历史预测统计，没有经济有效性或优胜结论。现可按 [共享组合层](PREDICTION_ALLOCATION.md) 运行同账户 B0/B1；后续按 [N03 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md) 验收训练代理并实现连续净效用训练。
