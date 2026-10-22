# qwb · 本地量化模型工作台

在自己电脑上训练一个 A 股量化模型：数据和算力都在本机，结果附带一份告诉你"哪里不可信"的报告。

## 安装

- Windows 发布包：解压后双击根目录的 `1_安装.bat`，不需要预先安装 Python 或 uv；安装器使用包内的 uv。源码目录直接运行 `install.bat` 时，才需要 PATH 中有 [uv](https://docs.astral.sh/uv/)。
- Linux：在 `quant-workbench/` 中运行 `sh install.sh`；完整发布包已附带 Linux uv，源码目录没有时脚本会自动下载。

完整发布包已经把 `ashare-tcost` 放在 qwb 同级目录。源码运行时，把 [ashare-tcost](../ashare-tcost) 放在同级目录，报告才会有扣成本检验。

## 使用

- Windows：双击 `start.bat`，浏览器打开 `http://127.0.0.1:8765`，填表、点开始。
- 命令行：`qwb run examples/run_cn_rank_5d.yaml`

两种方式等价：网页只是帮你写一份 YAML 配置（保存在 `runs/ui_configs/`）。没写的项用默认值，改过的项列在报告首页。先用"试运行（合成数据）"几秒钟走通一遍流程。

数据：`config.local.yaml` 里填 Tushare 兼容接口的 token 文件路径，然后 `python scripts/fetch_cn_daily.py` 拉取日线快照（约 1 小时）。

## 官网与模型社区

`website/` 提供产品官网、动态玻璃 Hero、模型广场、训练配方与参考实验展示，连接 `share_site/` 的登录和模型分享服务。

研究社区位于 `/community`：按模型分享、训练方法、业绩复盘和问题讨论分板块。登录后可在 `/community/new` 发帖，帖子支持关联模型、附训练配方和持续回复讨论。

- Windows：运行 `powershell -ExecutionPolicy Bypass -File website/start.ps1`，按终端地址访问。
- 手动：在 `website/` 执行 `npm ci` 和 `npm run build`，在项目根目录执行 `QWB_SITE_DEV=1 python -m uvicorn share_site.app:app --host 127.0.0.1 --port 8800`（本地预览）。部署到服务器时改为设置 `HF_OAUTH_CLIENT_ID`、`HF_OAUTH_CLIENT_SECRET`、`HF_OAUTH_REDIRECT_URI` 和 `QWB_SITE_SECRET`；两者都没设时站点拒绝启动。
- 设计参考、接口与部署说明：[website/DESIGN.md](website/DESIGN.md)。

## 报告首页会告诉你什么

| 常见错误 | 报告怎么处理 |
|---|---|
| 把 IC 当收益 | 第一节就是扣成本后的净超额：STRUCT 价差 + 佣金 + 印花税，涨跌停和停牌按规则成交 |
| 用了未来数据 | 时点数据契约；把数据截断在 t 重算特征和股票池，结果必须不变 |
| 把随机波动当模型差异 | 多种子，报告种子间预测相关和持仓重叠 |
| 挑最好看的结果 | 本地台账记下每次评估，报告列出同一测试期的全部配置 |
| 反复看测试集 | 第二次评估起首页警告"不能当作独立确认"；测试期改了也会标出 |
| 和弱基准比 | 反转、动量、低波动、截面打乱，与模型同场比较，扣成本口径相同 |

台账是本地文件，只支持诚实自报，不能证明没删过记录。

## 参考结果（A 股日频，剔除 ST，2024–2025 最终测试）

LightGBM 默认配置：Rank IC +0.110（t=8.65），扣成本后年化净超额 +2.7%（t=0.30），即排序能力显著，扣成本后不显著。见 `runs/cn_rank_5d_lgbm/report.md`（本地生成）。

## 不做什么

不托管训练、不转发数据、不排名、不承诺收益，不构成投资建议。
