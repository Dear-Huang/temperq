<div align="center">

# TemperQ

**训练你自己的模型，看清你的每一笔持仓。**

[English](README.en.md) · 中文

[![CI](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml/badge.svg)](https://github.com/Dear-Huang/temperq/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)

</div>

TemperQ 是一个在自己电脑上运行的 A 股量化建模工作台。不写代码，也能用自己的数据训练选股模型，看到扣除交易成本后的真实结果，再把模型接到自己的持仓上，每天得到参考建议并记入模拟账本。

多数量化工具从模型出发，回测就是终点。TemperQ 从账户出发：你现在持有什么，明天该买卖什么，成交后账户会变成什么样。

<p align="center">
  <img src="docs/img/train.png" width="49%" alt="训练页">
  <img src="docs/img/paper.png" width="49%" alt="纸面交易账户">
</p>

<p align="center"><sub>左：训练页。右：纸面账户（40 只股票的历史演示，不代表策略表现）。</sub></p>

## 为什么用 TemperQ

- **你自己的模型。** 用你的数据，在你的电脑上训练。数据和模型都不离开本机。
- **自带审稿人。** 时点数据、前视检查、简单基准对照、扣成本检验默认开启。报告第一页先说明扣掉交易成本后还剩多少，而不是只给好看的预测指标。
- **落到持仓。** T+1、整手、最低佣金、印花税、涨跌停、ST 从一开始就算在内。输入现金和持仓，得到按股数计算的买卖建议。
- **持续迭代。** 每次修改都生成新版本，旧版本保留；评价结果会说明差异是否超出随机波动，换不换由你决定。

## 四步流程

工作台默认打开“我的策略”。已有模型可以直接选用；没有模型时先训练。操作见 [我的策略指南](docs/STRATEGY_WORKSPACE.md)。

| 步骤 | 你做什么 | 得到什么 |
| --- | --- | --- |
| 1 训练或选用模型 | 用默认设置训练，或选择已有模型和数据源 | 冻结的打分来源 |
| 2 配置政策 | 选预设、填现金；需要时展开高级设置 | 可版本化的纪律、仓位与费用设置 |
| 3 历史检验 | 与同信号的立即调整对照 | 已计费用、交易规模、仓位和风险/收益差，不保证增益 |
| 4 纸面运行 | 从启用日开始检查新行情并记账 | 决策、模拟成交与受阻；按时记录和补算分开 |

当前每日检查只读取已经更新的数据源，不自动下载行情。“分步靠近目标”是自有研究机制的可选预设，尚未证明收益优势。原模型评价与纸面账户入口继续保留。

## 快速开始

**不写代码的 Windows 用户**：便携候选包已构建（约 108MB，自带 Python 和 CPU 依赖）。发布后从 [Releases](https://github.com/Dear-Huang/temperq/releases) 下载 `TemperQ-windows-x64.zip`，完整解压后双击 `start.bat`，无需 Python、uv、git 或 Node。当前验收状态与发布步骤见 [便携版交付说明](docs/WINDOWS_PORTABLE.md)；候选包尚待上传。

**从源码运行**：

需要 Windows 10/11 或 Linux、Python 3.10+，以及 [uv](https://docs.astral.sh/uv/)。LightGBM 只用 CPU，不需要显卡，建议 16GB 以上内存。

**Windows**

```powershell
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
.\install.bat
.\start.bat
```

**Linux**

```bash
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
sh install.sh
sh start.sh
```

浏览器会打开 `http://127.0.0.1:8765`。先选"试运行数据"跑通一遍，几秒钟就能看到第一份报告。

命令行与网页使用同一条训练链路：

```bash
temperq run examples/run_smoke.yaml
```

## 数据

TemperQ 不提供行情数据，你需要自备获得授权的数据。三种方式：

- **上传 CSV**：每行一只股票一天，含 `code, date, open, high, low, close, amount`，页面上有模板可下载。
- **Tushare**：填写自己的 token 后自动下载。
- **BaoStock（免费）**：训练页点击“一键下载免费数据”，无需注册、token 或积分。默认 40 股、2021 年至昨天；完成后继续训练。入门样本不代表全市场，见 [使用手册 · 零成本数据路径](docs/使用手册.md#九零成本数据路径)。

建议至少 3 年、720 个交易日的数据。训练前页面会提示数据是否足够。

## 它不做什么

- 不托管资金，不连接券商，不自动下单。
- 不荐股，不承诺收益，不做收益排名。
- 模拟账本目前只计佣金和印花税，尚未计入买卖价差与冲击成本，结果偏乐观。
- 历史检验通过只代表可以开始纸面观察，不代表能够实盘盈利。

## 文档

- [使用手册](docs/使用手册.md)：安装、训练、读报告、纸面交易、数据准备
- [产品理念](docs/PRODUCT_VISION.md)：为什么从持仓出发
- [为什么这样设计](docs/WHY_THIS_DESIGN.md)：端到端测过什么，为什么分信号、治理、账本三层
- [常见陷阱](docs/PITFALLS.md)：前视、隔夜跳空、把 IC 当收益等
- [开发说明](docs/DEVELOPMENT.md)：项目结构、测试、网站与部署

## 项目状态

早期版本（0.0.x），接口可能变化。已完成本地训练、独立评价、账户建议与每日模拟账本；真实前向记录刚开始积累，尚未经过外部用户试用。欢迎通过 [Issues](https://github.com/Dear-Huang/temperq/issues) 反馈问题。

## 许可

版权所有 © 2026 Dear-Huang。本项目以 [AGPL-3.0](LICENSE) 授权：可以自由使用、修改和再分发；修改后通过网络向他人提供服务，须同样公开修改后的源码。成本库 `third_party/ashare-tcost` 采用 MIT 许可，字体为 SIL OFL 1.1，详见 [NOTICE](NOTICE)。

本项目用于研究与学习，不构成投资建议。
