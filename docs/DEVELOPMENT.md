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
