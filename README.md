# Quant Workbench

让普通投资者在自己的电脑或服务器上，用数据训练、修改和比较 A 股模型，并逐步运行成每日可追踪的策略。工作台提供数据准备、训练、前视与成本检查、报告和模型分享；每日账户决策、自动更新与模拟运行仍在补齐，不能把现有参考选股清单视为完整账户交易系统。

**当前方向（2026-10-23）**：B0 LightGBM/Ridge 为产品主线，B1 原始行情 GRU 为第二模型，B2 净效用训练保留实验地位并暂停扩展。先完成 B0 的每日策略闭环，再做共同真实数据上的模型对照和发布验收。见 [方向入口](docs/specs/README.md)、[实施 SPEC](docs/specs/PRODUCT_WORKFLOW_SPEC.md) 与 [竞品/文献核验](docs/research/PRODUCT_DIRECTION_REVIEW_20261023.md)。下文及历史研究记录中的已运行实验不代表整个产品已经一键可用或具有收益优势。

定位与长期取舍见 [PRODUCT_VISION.md](docs/PRODUCT_VISION.md)。本轮最小入口：模型页“账户交易建议”支持现金与持仓 CSV；“模拟账户”查看日线账本，“更新日线数据”保存追加快照。操作和免费 BaoStock 路径见 [使用手册](docs/使用手册.md)。行情缺失时账本等待，不补造前向收益。

2026-10-23 最小交付：账户建议与每日模拟软件链路已验收；本机前向账户和每天 18:00 记账任务已建立。现有 API 已完成独立 40 股真实数据测试，行情补至 2026-10-22，LightGBM 训练、账户建议与 9 日历史回放通过；它不是全市场比较或真实前向成绩。原全市场快照仍截止 2025-12-31。任务不自动下载行情；免费 BaoStock 联网路径仍待验证。各步骤状态见 [实施 SPEC](docs/specs/PRODUCT_WORKFLOW_SPEC.md)。

**本目录是唯一开发主目录。** 不再到 `D:/huang/qwb_release_*` 中改代码。历史副本已归档，成本库已并入项目，GitHub 克隆无需另找同级仓库。整合记录见 [docs/INTEGRATION.md](docs/INTEGRATION.md)。

## 项目结构

```text
quant-workbench/
  src/qwb/                  训练、数据、报告、CLI、本地 UI
    bench/                  QW-Bench 评分、账户回放与证据检查
  website/                  React + Vite + Three.js 网站前端
  share_site/               FastAPI 模型社区、HF 登录和 SQLite 存储
  bench/                    评价规格、协议、真实实验说明
  third_party/ashare-tcost/  已纳入仓库的 STRUCT 成本库，独立 MIT 许可
  examples/                 任务卡、训练配置与 CSV 模板
  schemas/                  本地保留的 schema 目录（当前没有发布文件）
  tests/                    主项目自动测试
  scripts/                  数据准备、备份、检查与打包工具
  deploy/                   Docker、Compose、Caddy HTTPS 配置
  docs/                     用户手册、开发、整合与验证文档
  .github/workflows/        Windows/Linux 测试、前端和镜像构建 CI
  data/ models/ runs/        本地数据、权重、训练与 Bench 产物，不上传
  third_party/Kronos/       可选外部模型源码，不上传
  archives/                 旧发布目录与原成本库 Git 历史，不上传
  artifacts/                新生成的源码包/用户发布包，不上传
```

## Windows 快速开始

源码安装需要 [uv](https://docs.astral.sh/uv/)。本地用户发布包已附带 Windows/Linux uv。

```powershell
cd D:\huang\quant-workbench
.\install.bat
.\start.bat
```

本地工作台地址通常为 `http://127.0.0.1:8765`，端口占用时自动递增。先选合成数据试运行；真实训练须自行准备获授权的数据。

开发者使用 PowerShell 安装测试和网站依赖：

```powershell
.\install.ps1 -Dev
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q third_party/ashare-tcost/tests
.venv\Scripts\python.exe scripts/check_repository.py
```

安装器会重建 `.venv`；已有开发环境只需更新依赖时，见 [开发说明](docs/DEVELOPMENT.md)。安装 CPU 版 LightGBM 不需要 GPU；Kronos/PyTorch 是可选扩展。

## Linux 与服务器训练

```bash
cd /path/to/quant-workbench
sh install.sh                 # 开发用 --dev；网站依赖用 --with-site
sh start.sh --no-browser
```

训练 UI 仅监听回环地址。远程使用 `ssh -N -L 8765:127.0.0.1:8765 user@server`，再在本机打开该地址。Debian/Ubuntu 的 LightGBM 如缺运行库，安装 `libgomp1`。

CLI 与网页使用同一训练链路：

```bash
qwb run examples/run_smoke.yaml
qwb run examples/run_cn_rank_5d.yaml --check
qwb run examples/run_cn_rank_5d.yaml
```

真实数据设置：复制 `config.example.yaml` 为 `config.local.yaml`，填写本机数据/缓存路径和 token **文件路径**。现有本机配置会保留，不会被整合覆盖。数据下载可运行 `python scripts/fetch_cn_daily.py`，或使用本地 UI 的 CSV/API 导入。完整步骤见 [使用手册.md](docs/使用手册.md)。

## 网站与社区

网站 `website/` 由 `share_site/` 提供页面、模型元信息、论坛与 Hugging Face OAuth。前端和后端部署在同一域名下。

Windows 本地预览：

```powershell
.\install.ps1 -WithSite
cd website
npm ci
npm run build
cd ..
powershell -ExecutionPolicy Bypass -File website/start.ps1
```

预览地址由启动脚本输出，一般为 `http://127.0.0.1:8800`。Linux 本地预览：

```bash
sh install.sh --with-site
npm --prefix website ci
npm --prefix website run build
QWB_SITE_DEV=1 .venv/bin/python -m uvicorn share_site.app:app --host 127.0.0.1 --port 8800
```

正式服务器部署使用 [deploy/README.md](deploy/README.md)：提供 Docker 多阶段构建、单进程后端、持久化数据库、健康检查和 Caddy 自动 HTTPS。部署前填写域名、HF OAuth 与随机会话密钥。**公开服务器不能启用 `QWB_SITE_DEV=1`。**

## 训练、分享、Bench 的衔接

1. 训练：UI/`qwb run` 将每次尝试归档到 `runs/history/<run_id>/`；成功后刷新兼容入口 `runs/<名称>/`，保留同名重跑历史。
2. 分享：用模型包与模型卡描述产物，在 HF 模型仓库保存可分发文件；网站读取固定提交的卡片与元信息，不下载执行模型代码。
3. 评价：`qwb bench model` 读取运行目录/预测文件，在冻结的原始快照与参考模型上独立重算标签、诊断和研究账户。
4. 追踪：本地“实验记录”关联训练版本、折预测、最终模型和独立 Bench，支持配对比较和导出不含数据/权重的研究摘要。训练与网页评价由持久化 worker 执行。

```bash
qwb bench init runs/cn_rank_5d_lgbm --data-dir data/snapshots/cn_daily --output bench/protocols/my_development.yaml
qwb bench model runs/cn_rank_5d_lgbm --protocol bench/protocols/my_development.yaml --output runs/bench/my_model_001
qwb bench verify runs/bench/my_model_001 --inputs
qwb bench traps --help
```

完整操作、后台任务、兼容与评价边界见 [研究闭环说明](docs/RESEARCH_WORKFLOW.md)。网站尚未自动接收模型并排队执行 Bench；本地工作台已支持冻结协议、运行 Bench 和比较。当前 Bench 是历史开发评价，不是实盘认证。已有 `bench/protocols/cn_rank_5d_development_v*.yaml` 固定原机器路径与数据哈希；换机器须按 [bench/README.md](bench/README.md) 建立新协议。原始行情和参考训练产物不随 GitHub 仓库分发。

## GitHub 与发布

上传前运行：

```powershell
.venv\Scripts\python.exe scripts/check_repository.py
git status --short
git diff --check
```

确认后使用现有 `main` 分支提交并配置自己的远程地址：

```powershell
git add .
git commit -m "Consolidate workbench, cost library and deployment setup"
git remote add origin https://github.com/YOUR_ACCOUNT/quant-workbench.git
git push -u origin main
```

已有 `origin` 时先 `git remote -v`，再按实际地址调整。不要强推。CI 会检查 Python、网站构建和服务器镜像。

生成包含网站、部署文件、CI、成本库的完整源码包：

```powershell
.venv\Scripts\python.exe scripts/build_release.py --source-only
```

生成本地工作台用户包（不包含服务器网站）：

```powershell
.venv\Scripts\python.exe scripts/build_release.py --tools-dir archives/releases/qwb_release_1022e/qwb/tools
```

产物在 `artifacts/releases/<类型-时间>/`；后续可以将 uv 缓存在忽略的 `tools/`，再使用 `--tools-dir tools`。脚本包括尚未提交的新源码，并检查敏感文件和大文件，附 SHA-256 清单；不会覆盖历史输出。源码包用于传输源码，保留 Git 历史应使用 Git 推送。

## 持续开发

后续开发从 [产品方向入口](docs/specs/README.md) 和 [产品闭环实施 SPEC](docs/specs/PRODUCT_WORKFLOW_SPEC.md) 开始：先打通 B0 的账户状态、每日建议、数据更新、模拟记账和策略看板，再用共同真实数据评价 B1 与成熟配方。外部用户试用、硬件测量和服务器验收是发布要求。

从 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 了解模块职责、测试、依赖锁和下一步路线；从 [docs/VALIDATION.md](docs/VALIDATION.md) 了解本次验证范围。网站设计与交互背景见 [website/DESIGN.md](website/DESIGN.md)，评价边界见 [bench/SPEC.md](bench/SPEC.md)。

本轮 [竞品与文献核验](docs/research/PRODUCT_DIRECTION_REVIEW_20261023.md) 修正了部分旧评价，明确哪些是能力、哪些仍是目标。专业用户可在自己的框架训练后提交标准预测；默认模型不限制外部研究路径。

原始行情序列、目标账户与 B2 的开发资料保留在 [研究实施记录](docs/E2E_IMPLEMENTATION.md)、[序列训练](docs/PIT_SEQUENCE_RUNS.md)、[目标 Bench](docs/TARGET_BENCH.md) 和 [实验效用训练](docs/UTILITY_TRAINING.md)。B2 暂停扩展，已有合成验证及未关闭问题保留，不作为产品已盈利或已部署的证据。

MIT 许可见 [LICENSE](LICENSE)，成本库保留其 [独立许可](third_party/ashare-tcost/LICENSE)。数据和模型权重的授权需分别核对。项目用于研究与复现，不提供收益承诺或自动实盘交易。
