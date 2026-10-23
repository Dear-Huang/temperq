# 预测到目标持仓的共享组合层

本地入口将冻结的日内 Ridge/GRU 预测接入同一风险/成本组合层，再交给独立目标 Bench。它使用实际账户状态反馈，不重训或重选预测模型。设计见 [ADR 005](specs/ADR_005_PREDICTION_ALLOCATION.md)，后续训练见 [N03 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md)。

## 环境与输入

先按 [研究环境说明](E2E_IMPLEMENTATION.md) 安装 `requirements-e2e.lock` 和本地包。命令从仓库根目录执行；下面是 Windows 干净 CPU 环境，Linux 路径需改为 `bin/python`，Linux 尚未实测。

需要已完成且校验通过的 `e2e_prediction_v1` 归档、与训练内容身份完全相同的 PIT bundle、显式执行协议和组合配置。任务须是 `cn_predict_intraday_v1`；不能传旧五日排名模型。账户协议须遵守 [目标 Bench](TARGET_BENCH.md)，股票上限/最低现金必须与组合配置一致；真实数据的执行价格/日历/限制由用户提供。

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli bench allocate <冻结预测归档> --protocol <账户协议.json> --data-dir <PIT目录> --config examples/allocation_intraday_v1.yaml --output artifacts/allocation-new
artifacts/environments/e2e-clean/Scripts/python.exe -m qwb.cli bench allocation-verify artifacts/allocation-new --inputs
```

输出必须为新目录。`--inputs` 检查原始文件与整份源归档；即使不传，也会重新执行冻结策略与独立账户核对。可用 `--data-dir <搬迁后的同内容PIT目录>` 重算搬迁数据；`--inputs` 仍要求记录的原始路径存在且内容一致。重算需要相同实现及数值环境，更新代码后须用原发布版本核验历史结果。

## 合成复现

先运行序列演示，再复用其全部冻结预测，不挑选更好的种子或模型：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_sequence_demo.py --output artifacts/sequence-demo-new
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_allocation_demo.py --sequence-demo artifacts/sequence-demo-new --initial-holdings --output artifacts/allocation-demo-new
```

运行前保存 `plan.json`；所有结果及失败保留。`--initial-holdings` 按首日可见价格持有前三个合资格资产，每只至多 9%，总初始权益 100 万元；不是用未来收益挑选持仓。省略该选项时从现金开始，当前合成预测在既定成本下可能一直选择现金，须保留该负结果。该脚本只接受明确标为 synthetic 的数据；普通工作日生成和最后交收扩展只用于合成样例，不能当真实交易所日历。

## 配方、单位与失败

示例配置 [allocation_intraday_v1.yaml](../examples/allocation_intraday_v1.yaml) 使用 60 日历史、至少 20 个共同完整日内小数收益，固定协方差收缩与特征值下限。`root.T @ root` 等于审计协方差。成交额单位为人民币，费用以 bp 声明后转换成小数；按真实权益/历史正成交额中位数估算线性流动性成本。参数是演示配方，不代表校准过的真实市场冲击或历史税制。

现有持仓退出池后保留、禁止增持并显式赋零预测；不能因为无信号而消失。缺预测、缺风险/流动性历史或约束不可行时记录结构化失败并继续持仓。v2 应收占用预算但不提供现金。未知生命周期事件和已有挂单继续按目标 Bench 边界拒绝。

`output_policy` 必须事先选定。`strict_raw` 拒绝非法原始解；`highs_l1_projection_v1` 只在相同约束内作容差内的显式 L1 投影，保留原始/生产解，不做反向传播。不得将输出修复隐藏在验证端。

## 产物与读取

| 文件 | 内容 |
| --- | --- |
| `inputs.json`、`prediction_source/` | 原始身份、必要的冻结预测/训练元信息；PIT 数据仅保存身份与路径，不复制分发 |
| `protocol.json`、`allocation.yaml`、`plan.json` | 初始账户、全部执行条件、配方、实现/环境与数据身份 |
| `attempts.json` | 全部决策的执行状态，包括失败、取消和未运行 |
| `allocation_audit.json` | 逐日风险/成本/原始解/生产映射、实际状态及代理差异 |
| `submission.json`、`account/` | 生成目标、独立实际成交/权益/费用及重算证据 |
| `result.json`、`report.md`、`checksums.json` | 运行状态、证据等级、汇总诊断、文件内容校验 |

`COMPLETED` 表示运行与独立账户核对完成，仍可能有结构化决策失败；必须另看 `failed_decisions`。`threshold_exceedances=0` 仅表示所声明的按权益计量费用差与换手差未超限，不证明成本相对误差小，也不证明策略有收益。

## 已观察结果与下一步

当前证据 `artifacts/allocation-demo-20261023-004/comparison.json`：B0/B1 各 29 个目标，无失败；同一初始持仓下实际路径完全相同，净收益约 0.3487%，费用 250.08 元。成交代理费用仅 68.37 元，而意图代理几乎为零；精确执行还受开盘漂移、整数手、最低费用与未成交影响。需按 N03 SPEC 分项验证，不能把差异都归因于模型。

首个 strict_raw 演示和全现金演示均保留；没有按收益挑选输出策略或重训模型。所有这些结果都是 `SYNTHETIC_ONLY`，没有真实市场模型优势、容量或投资有效性结论。连续净效用训练、冻结多种子经济比较、Linux 和外部复现仍待完成。
