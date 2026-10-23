# 开发与持续优化

下一阶段研发先读 [端到端研究平台 SPEC](specs/E2E_RESEARCH_PLATFORM_SPEC.md) 与 [选型调研](research/E2E_RESEARCH_REVIEW_20261022.md)。从 M0 的时点、状态、目标持仓契约与环境验证开始，再进入表示学习和决策训练；新规范中的配置与接口尚不可执行。现有实现与兼容边界仍以本文及 [研究闭环](RESEARCH_WORKFLOW.md) 为准。

## 环境与依赖

基准环境为 Python 3.11、Node.js 22。CPU LightGBM 是默认链路；GPU/PyTorch/Kronos 按需安装。安装器会重建虚拟环境，已有环境增量更新可用：

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.lock
uv pip install --python .venv/Scripts/python.exe --no-deps -e . -e third_party/ashare-tcost
```

Linux 将 Python 路径换成 `.venv/bin/python`。成本库是本地独立 Python 包；直接 `pip install -e .` 不会自动安装它，必须同时安装这两个路径。

依赖以 `pyproject.toml` 声明、锁文件固定。`requirements.lock` 是训练依赖，`requirements-site.lock` 加网站依赖，`requirements-dev.lock` 加测试依赖。前端使用 `website/package-lock.json` 与 `npm ci`。

```bash
uv pip compile pyproject.toml --python-version 3.11 --universal -o requirements.lock
uv pip compile pyproject.toml --extra site -c requirements.lock --python-version 3.11 --universal -o requirements-site.lock
uv pip compile pyproject.toml --extra dev -c requirements.lock --python-version 3.11 --universal -o requirements-dev.lock
```

更新训练锁会改变模型环境，须重跑测试和参考实验后再更新说明。现有研究产物必须保留原环境记录。

## 模块职责

| 修改目标 | 入口与相关文件 |
| --- | --- |
| 任务/训练参数、命令行 | `src/qwb/cli.py`、`runconfig.py`、`taskcard.py`、`examples/` |
| 本地训练 UI、后台任务 | `src/qwb/ui.py`、`experiment_ui.py`、`jobs.py`、`worker.py` |
| 训练版本、评价关联与摘要导出 | `experiments.py`、`evidence_export.py`；契约见 `docs/RESEARCH_WORKFLOW.md` |
| 行情快照与数据导入 | `dataset.py`、`fetch.py`、`csvimport.py`、`connectors/` |
| 特征、训练、评估报告 | `daily.py`、`folds.py`、`pipeline.py`、`metrics.py`、`leakcheck.py`；内层历史选择与外层预测分离 |
| 成本估计与交易规则 | `netcost.py`、`rules/`、`third_party/ashare-tcost/` |
| 模型包/模型卡 | `package.py`、`card.py`、`consistency.py` |
| Bench 文件契约与回放 | `src/qwb/bench/`，先读 `bench/SPEC.md` |
| 官网与社区页面 | `website/src/`，后端 `share_site/app.py`、`forum.py` |
| 服务器与发布 | `deploy/`、`scripts/build_release.py`、`check_repository.py` |

训练与 Bench 通过运行目录衔接。标准预测列：`code`、`decision_time`、`task`、`prediction`、`model_version`、`data_snapshot_id`。不要在网站页面重新实现评分逻辑；后续任务服务应调用统一训练/评分入口，并保存完整协议与证据。

本轮方法依据、真实开发对照与专业用户能力矩阵见 [专业研究链路审查](PROFESSIONAL_RESEARCH_AUDIT_20261022.md)。修改数据加载时须同时核对 `data_manifest`、Bench 协议清单、执行状态覆盖和评分实现哈希。原生训练目标校验不能因任务卡 schema 能表达更多任务而省略。

## 本地验证

在项目根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q third_party/ashare-tcost/tests
.venv\Scripts\python.exe -m qwb.cli run examples/run_smoke.yaml
.venv\Scripts\python.exe scripts/check_repository.py
git diff --check
npm --prefix website ci
npm --prefix website run build
```

前端开发需要网站后端在 8800，然后在 `website` 运行 `npm run dev`。Vite 会代理 API、登录、分享、下载和社区路由。

已有浏览器验收脚本：网站运行于 8800 时，在 `website` 执行 `node qa.mjs`；论坛使用 `python website/qa_forum_server.py` 创建独立临时数据库、监听 8801，再执行 `node qa-forum.mjs`。它们使用 Playwright 的 Chrome 通道。不要向真实社区写入测试内容。

常规 QA 截图只写入 `website/test-results/`。只有明确设置 `QWB_UPDATE_POSTER=1` 时才更新发布用 `public/hero-poster.png`，避免验收过程产生媒体文件改动。

## Git 工作流

从主目录建功能分支，小范围修改、运行相关测试、检查 diff 后提交。不要提交 `.venv`、token、真实行情、模型权重、`runs/`、`archives/`、`artifacts/` 或 `node_modules/`。检查工具会检查当前已跟踪和未忽略的新文件，误加入 Git 的私有目录也会报错。

CI 当前定义 Windows/Linux Python 测试、成本库测试、合成训练、源码检查、网站构建和 Docker 构建。Docker 构建依赖网络下载基础镜像与依赖；CI 成功不等于服务器域名、OAuth 和备份已经配置。

## 优化顺序

2026-10-23 起以 [产品闭环 SPEC](specs/PRODUCT_WORKFLOW_SPEC.md) 为当前顺序：先完成 B0 账户建议、更新/每日推理、模拟账本和看板，再做 B1 真实数据与成熟配方比较、版本迭代和发布验收。下列工程事项按该顺序安排；B2 研究暂停扩展，原有 [实施记录](E2E_IMPLEMENTATION.md) 继续保留。

1. 当前训练版本、网页 Bench 与比较、摘要导出及独立 worker 已贯通，契约见 [研究闭环说明](RESEARCH_WORKFLOW.md)。接下来补齐折级训练恢复、资源预算和下载/体检 worker。
2. 固定平台契约：给任务卡、模型包、训练配置、Bench 协议定义可迁移版本；现有 v0.x 开发协议与正式版本区分保存。
3. 补齐队列与隔离：服务器训练与公共网站分进程/分权限运行，明确并发、资源配额、取消、超时与日志保留。
4. 完善证据：优先核验真实价格、状态、停牌/退市估值缺口，再扩大同配置多种子对照与冻结前向确认，随后补充容量、风险和完整搜索预算。当前信号分数不能替代实盘准入。
5. 完善运维：验证数据库备份恢复、日志与可用性监控；规模增长时再评估 PostgreSQL 与分布式任务服务。

现有 Markdown 使用手册已适配新目录；`docs/使用手册.docx` 保留为旧版文档快照。后续重新生成并渲染核对 Word 后再更新该文件，本次发布以 Markdown 为准。

---

# 附：项目结构、部署与发布（原 README 开发部分）

### 项目结构

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

### Windows 快速开始

源码安装需要 [uv](https://docs.astral.sh/uv/)。本地用户发布包已附带 Windows/Linux uv。

```powershell
git clone https://github.com/Dear-Huang/temperq.git
cd temperq
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

### Linux 与服务器训练

```bash
cd /path/to/quant-workbench
sh install.sh                 # 开发用 --dev；网站依赖用 --with-site
sh start.sh --no-browser
```

训练 UI 仅监听回环地址。远程使用 `ssh -N -L 8765:127.0.0.1:8765 user@server`，再在本机打开该地址。Debian/Ubuntu 的 LightGBM 如缺运行库，安装 `libgomp1`。

CLI 与网页使用同一训练链路：

```bash
temperq run examples/run_smoke.yaml
temperq run examples/run_cn_rank_5d.yaml --check
temperq run examples/run_cn_rank_5d.yaml
```

真实数据设置：复制 `config.example.yaml` 为 `config.local.yaml`，填写本机数据/缓存路径和 token **文件路径**。现有本机配置会保留，不会被整合覆盖。数据下载可运行 `python scripts/fetch_cn_daily.py`，或使用本地 UI 的 CSV/API 导入。完整步骤见 [使用手册.md](docs/使用手册.md)。

### 网站与社区

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
TEMPERQ_SITE_DEV=1 .venv/bin/python -m uvicorn share_site.app:app --host 127.0.0.1 --port 8800
```

正式服务器部署使用 [deploy/README.md](deploy/README.md)：提供 Docker 多阶段构建、单进程后端、持久化数据库、健康检查和 Caddy 自动 HTTPS。部署前填写域名、HF OAuth 与随机会话密钥。**公开服务器不能启用 `TEMPERQ_SITE_DEV=1`。**

### 训练、分享、Bench 的衔接

1. 训练：UI/`temperq run` 将每次尝试归档到 `runs/history/<run_id>/`；成功后刷新兼容入口 `runs/<名称>/`，保留同名重跑历史。
2. 分享：用模型包与模型卡描述产物，在 HF 模型仓库保存可分发文件；网站读取固定提交的卡片与元信息，不下载执行模型代码。
3. 评价：`temperq bench model` 读取运行目录/预测文件，在冻结的原始快照与参考模型上独立重算标签、诊断和研究账户。
4. 追踪：本地“实验记录”关联训练版本、折预测、最终模型和独立 Bench，支持配对比较和导出不含数据/权重的研究摘要。训练与网页评价由持久化 worker 执行。

```bash
temperq bench init runs/cn_rank_5d_lgbm --data-dir data/snapshots/cn_daily --output bench/protocols/my_development.yaml
temperq bench model runs/cn_rank_5d_lgbm --protocol bench/protocols/my_development.yaml --output runs/bench/my_model_001
temperq bench verify runs/bench/my_model_001 --inputs
temperq bench traps --help
```

完整操作、后台任务、兼容与评价边界见 [研究闭环说明](docs/RESEARCH_WORKFLOW.md)。网站尚未自动接收模型并排队执行 Bench；本地工作台已支持冻结协议、运行 Bench 和比较。当前 Bench 是历史开发评价，不是实盘认证。已有 `bench/protocols/cn_rank_5d_development_v*.yaml` 固定原机器路径与数据哈希；换机器须按 [bench/README.md](bench/README.md) 建立新协议。原始行情和参考训练产物不随 GitHub 仓库分发。

### GitHub 与发布（维护者）

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
git remote add origin https://github.com/YOUR_ACCOUNT/temperq.git
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

### 持续开发

后续开发从 [产品方向入口](docs/specs/README.md) 和 [产品闭环实施 SPEC](docs/specs/PRODUCT_WORKFLOW_SPEC.md) 开始：先打通 B0 的账户状态、每日建议、数据更新、模拟记账和策略看板，再用共同真实数据评价 B1 与成熟配方。外部用户试用、硬件测量和服务器验收是发布要求。

从 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 了解模块职责、测试、依赖锁和下一步路线；从 [docs/VALIDATION.md](docs/VALIDATION.md) 了解本次验证范围。网站设计与交互背景见 [website/DESIGN.md](website/DESIGN.md)，评价边界见 [bench/SPEC.md](bench/SPEC.md)。

本轮 [竞品与文献核验](docs/research/PRODUCT_DIRECTION_REVIEW_20261023.md) 修正了部分旧评价，明确哪些是能力、哪些仍是目标。专业用户可在自己的框架训练后提交标准预测；默认模型不限制外部研究路径。

原始行情序列、目标账户与 B2 的开发资料保留在 [研究实施记录](docs/E2E_IMPLEMENTATION.md)、[序列训练](docs/PIT_SEQUENCE_RUNS.md)、[目标 Bench](docs/TARGET_BENCH.md) 和 [实验效用训练](docs/UTILITY_TRAINING.md)。B2 暂停扩展，已有合成验证及未关闭问题保留，不作为产品已盈利或已部署的证据。

版权所有 © 2026 Dear-Huang。本项目以 [AGPL-3.0](LICENSE) 授权：可以自由使用、修改和再分发；修改后通过网络向他人提供服务，须同样公开修改后的源码。成本库 `third_party/ashare-tcost` 保留其 [MIT 许可](third_party/ashare-tcost/LICENSE)，字体为 OFL 1.1，详见 [NOTICE](NOTICE)。数据和模型权重的授权需分别核对。项目用于研究与复现，不提供收益承诺或自动实盘交易。
