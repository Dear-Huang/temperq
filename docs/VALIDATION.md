# 整合验证记录

日期：2026-10-22。验证基准：Windows、Python 3.11.15、Node.js；干净环境位于忽略的 `artifacts/verification-env/`。

## 已完成

| 检查 | 结果 |
| --- | --- |
| 主项目完整测试 | 219 passed；1 项现有 Starlette/httpx 弃用提示 |
| 新增部署路径与健康检查测试 | 与发布路径检查一起定向运行，32 passed（含上述完整测试采集后增加的 2 项测试） |
| 成本库 | 7 passed，从 `third_party/ashare-tcost` 导入 |
| 干净 Python 环境 | 从 `requirements-dev.lock` 安装全部依赖及两个本地包成功 |
| 主 `.venv` | 结束旧进程后同步锁定依赖，`uv pip check` 通过；导入成本库/FastAPI/Pydantic 成功 |
| 合成训练 | `run_smoke.yaml` 完成并写出报告，只用于链路验收 |
| 网站依赖与构建 | `npm ci` 成功，依赖审计 0 漏洞；`npm run build` 成功 |
| 网站浏览器验收 | Playwright：桌面 1440/1920、手机 390/360；无页面错误、无横向溢出 |
| 3D 与媒体 | 非空/彩色 WebGL 像素、场景边界、暂停、减弱动效、PNG 回退通过，已查看截图 |
| 社区浏览器验收 | 临时数据库发布、回复、刷新持久化、搜索、HTML 转义、桌面/手机通过 |
| 本地服务 | `/healthz` 返回 `status=ok`；预览使用 `artifacts/site-preview/site.db`，不写真实社区数据库 |
| 安装器 | Bash 语法、PowerShell 解析、Windows/Linux `--dev`/`-Dev` dry-run 通过 |
| Compose | YAML 可解析，后端不公开端口，数据库持久化路径与卷声明匹配 |
| 发布检查 | 未发现私有目录、疑似凭据或超过 GitHub 限制的大文件；归档不进入源码清单 |
| Git 历史检查 | 27 个提交、246 个历史文件版本，未发现疑似凭据或超过 100 MB 的文件 |
| 完整源码包与用户包 | ZIP 完整性与全部清单文件 SHA-256 匹配；新增 Bench/成本库源码完整，用户包带 Windows/Linux uv |
| 用户包独立安装 | 从导出目录构建并安装两个 wheel 成功，规则 YAML 资源存在，合成训练完成 |
| 旧历史 | 成本库原 Git 历史可读取；8 个旧发布/验收目录都位于主目录内 |

浏览器截图和 JSON 位于忽略的 `website/test-results/`，干净验证环境、恢复 wheel 和本地预览数据库位于忽略的 `artifacts/`。这些文件不进入 GitHub 或发布包。

## 边界

本机没有 Docker，Linux 安装器仅做语法与 dry-run 检查。真实 Linux 容器启动、Compose、Caddy 证书、DNS 和 HF OAuth 必须在 GitHub CI/目标服务器验收。

没有重跑多年的真实行情训练或改变历史 Bench 分数。网站到服务器训练/自动 Bench 队列尚未实现。没有创建远程 GitHub 仓库、推送提交或登录真实服务器；本次目标是整合并完成可上传/可部署的前期准备。

## 同日研究闭环优化验收

以下是整合之后的新增验收记录，前述表格保留为初次整合基准。操作说明见 [RESEARCH_WORKFLOW.md](RESEARCH_WORKFLOW.md)。

| 检查 | 结果 |
| --- | --- |
| 主项目完整回归基准 | 231 passed；运行约 447 秒，随后增加了针对性测试 |
| 后续核心定向回归 | `test_experiments.py`、`test_runconfig.py`、`test_jobs.py`、`test_bench.py`：58 passed，约 217 秒 |
| 最后兼容与发布回归 | `test_bench.py`、`test_card.py`、`test_repository_export.py`、成本库测试：58 passed，约 88 秒；含新增旧账户完整性模式测试 |
| 版本保留与绑定 | 真实小型训练、同名重跑、失败/取消保留、跨工作区保留、篡改拒绝、历史折与最终模型版本区分、Bench 与训练绑定通过 |
| 后台任务 | 独立 worker、活进程恢复保护、UI/CLI 入口、失败重试通过；关闭网页后子进程继续运行；未实现折级恢复或系统重启后的常驻调度 |
| 完整合成演示 | `artifacts/research-demo-20261022-v4/`：420 日 × 40 股票，LightGBM/Ridge、冻结协议、两次独立账户、配对比较、摘要 ZIP 完成 |
| 本地工作台浏览器 | 最新代码与 v4 演示：1440 桌面、390/360 手机，12 张截图，网页后台 Bench、比较、下载通过，errors=[]，无横向溢出；已目视检查桌面比较和手机选择页面 |
| 本地真实旧训练导入 | 5 份运行复制到 `runs/history/`，均标 `legacy_import` 并校验；原目录保留，不补造原训练时间/代码身份 |
| 历史 Bench 完整性 | v0.3 replay、v0.3 verified、v0.4 lifecycle：`--integrity-only` 均 PASS；不修改旧文件 |
| 历史 Bench 算术 | v0.3 verified、v0.4 lifecycle：默认验证均 PASS，重算逐日均值与账户；本次未用 `--inputs` 重查真实输入 |
| 早期账户协议 | v0.3 replay 缺 `history_eligibility`，当前完整验证拒绝；可查文件完整性，算术复核需原评分环境，不能据此直接参与当前版本比较 |
| 研究摘要 | 只输出白名单 Markdown/JSON，检查绑定与哈希；不携带行情、权重、代码或本机路径；非有限统计值转 null |
| 发布文件竞争 | 用户包验收发现百度网盘同步在暂存目录生成 sidecar；打包已改为仅使用显式清单，并在入 ZIP 前复查内容哈希。`test_repository_export.py`：17 passed，含复制期间插入同步文件的针对性测试 |

上述测试批次有重叠，不能相加作为唯一测试数。截图及下载位于忽略的 `artifacts/workbench-qa/`。本地真实工作台为 `http://127.0.0.1:8765/experiments`，隔离合成演示为 `http://127.0.0.1:8766/experiments`。

当前闭环覆盖本地训练、历史版本、独立评价、比较和手动导出分享。公开网站未接入自动训练、自动认证或结构化 Bench 上传。真实行情的北交所异常、退市估值、前向验证、容量和净超额统计分辨率问题仍见 `bench/REAL_RUN_V0_4.md`；合成示例和工程检查不能证明真实策略增量收益。Linux/Docker、OAuth、真实服务器部署仍受前述验收边界约束。

## 同日训练质量与专业研究增强

方法、已核验的竞品官方代码、文献证据层级、专业用户边界与完整数字见 [专业研究链路审查](PROFESSIONAL_RESEARCH_AUDIT_20261022.md)。以下记录属于后续一轮，不覆盖上述历史验收。

| 检查 | 结果 |
| --- | --- |
| 选择与时间隔离 | 改变整个外层测试期 OHLC，选中轮数与保存折模型不变；核对内层训练标签成熟、清除样本、历史尾窗和最终重拟合记录 |
| 数据内容与异常 | 训练中修改输入导致失败且不发布；组合来源、历史指数/自选池、来源删除再导入、状态覆盖缺失与非法价格有针对性回归 |
| 信号推断 | 同样本配对差、完整日 IC 覆盖、相同参考不构成增量，以及缺失日期的 Newey-West 滞后距离有回归 |
| 真实开发对照 | 240 只历史代码哈希抽样、2018–2023 行情、2021–2023 外层年折、两个种子；固定 300 轮对比同参数历史内层选轮数；未选择其他验证窗 |
| 最终评分与对账 | 固定原训练归档和协议，当前评分器重评双方；`verify_result(inputs=True)` 均通过，评分实现哈希匹配当前代码；原评分目录保留 |
| 实测信号 | 721 日，平均 Rank IC 0.06375 -> 0.08370；配对增量 0.01995，95% 日期块区间 [0.01089, 0.02843]；仅限当前开发子集 |
| 实测经济边界 | 基础费用下新减旧年化净增量 847.85 bp，95% 区间 [-61.76, 1779.88]；存在估值/数据缺口，两组均 DIAGNOSTIC_ONLY，不授予净改善结论 |
| 合成完整链路 | `artifacts/research-demo-20261022-v5/` 完成训练、schema 5 协议、Bench、比较、白名单摘要；只证明工作流可用 |
| 浏览器 | 最终 UI：1440 桌面、390/360 手机，12 张截图，网页后台 Bench、比较和下载 PASS，errors=[]；修复手机协议哈希单字列，已查看桌面/手机截图 |
| 成本库 | 7 passed；未修改成本库模型 |
| 最终主项目完整回归 | 262 passed，476.57 秒；仅一项现有 Starlette/httpx 弃用提示，包含上述删除接口与 UI 修复 |

真实报告位于忽略的 `artifacts/model-quality-20261022/QUALITY_AUDIT_FINAL.json`，含当前 `bench/*-final` 和 `comparison-final` 路径。冻结训练早于最终评分代码，重评没有重训或更新原训练身份。原始供应商时点、修订历史和完整退市覆盖未独立认证；本次不是新市场前向确认。原生分类/波动率/直接持仓任务明确拒绝，不能因任务卡可描述就认为已经实现。

初次本轮完整测试为 256 passed、1 failed（旧可用时间文字断言）；后续 117 项定向测试为 116 passed、1 failed，继续暴露了来源删除流程中写死旧编号的问题。修复了测试对实际编号的读取以及 UI 删除接口对新编号的兼容。最终回归结果另记于下文，不把前述失败批次称为全通过，也不累加重叠测试数。

本轮交付目录为 `artifacts/releases/20261022-quality-source/` 与 `artifacts/releases/20261022-quality-local/`。源码包包含网站、部署和 CI；本地用户包附 Windows/Linux uv，不包含网站服务。发布验收使用 ZIP CRC、精确文件集合、逐文件 SHA-256 和当前源码一致性检查，结果写入 `artifacts/quality-release-verification.json`。独立环境位于 `artifacts/verification-env/`，从导出源码重装两个本地包后在隔离目录运行合成训练。发布包、验收产物、真实市场数据与权重不上传 GitHub。Docker/Linux、OAuth 与真实服务器验收边界保持前述说明。

## 2026-10-23 端到端基础与序列运行

路线与后续门槛见 [SPEC v0.2](specs/E2E_RESEARCH_PLATFORM_SPEC.md)，逐项实测及产物见 [实施记录](E2E_IMPLEMENTATION.md)。本轮未重训真实市场模型或改变历史经济结论。

| 检查 | 结果与范围 |
| --- | --- |
| 干净 Windows CPU 环境 | 从 `requirements-e2e.lock` 新建隔离环境，无系统 site-packages/原环境 `.pth`；58 个第三方包、两个本地包安装完成，`uv pip check` 通过，torch 2.11.0+cpu |
| 数值与手算账户 | 最新 `e2e-m0-windows-clean-20261023-002/acceptance.json` 为 PASS；闭式解误差约 2.27e-14、原始残差约 2.78e-11、三个有限差分步长均通过；手算现金/权益/费用及账本恒等通过 |
| 完整回归 | 336 passed，无失败，566.99 秒；35 条现有 Starlette/httpx 及 CVXPYlayers/NumPy 弃用提示 |
| 最终针对性回归 | 配置、序列训练、掩码、历史时点/修订、目标 Bench、原始股数账户：45 passed，143.02 秒；覆盖完整回归启动后的小幅接口修正，批次有重叠 |
| 三种子预测流程 | 冻结三折/60 日/24 资产 B0 Ridge 与 B1 GRU；696 条匹配预测，18 次拟合、全部 checkpoint 重载一致；合成数据无模型优胜结论 |
| 目标持仓 CLI | 纯 JSON 生成、独立实际账户回放、`target-verify --inputs` 均通过；支持 T+1/交易单位/未成交，拒绝非空公司行为与已有跨日挂单 |
| 发布与文档 | 228 个源码文件扫描无错误；10 份主要 Markdown 本地链接无断链；完整源码包将包含新 SPEC、接口、依赖锁、示例、测试、网站和部署文件 |

完整回归记录 `artifacts/full-e2e-regression-20261023.xml`，最终针对性记录 `artifacts/final-focused-e2e-20261023.xml`。求解器 Windows wheel 来自本机先前 MSVC/SDK 构建缓存；不是未配置编译工具的新机器安装证明。Linux 未实际运行，本机 WSL 组件未启用，CI 文件存在不算跨平台通过。M2 公司行为、M3 连续效用训练、M4 冻结经济比较和 M5 外部复现仍未完成；源码包可审查不等于投资系统可用。

## 2026-10-23 公司行为与应收账本

后续 N01 实现使用新版账户/目标/协议，保留 v1 原解释，细节见 [ADR 004](specs/ADR_004_ACCOUNT_LIFECYCLE.md)。

| 检查 | 结果与范围 |
| --- | --- |
| 完整回归 | 干净 CPU 环境 359 passed、无失败，572.73 秒；35 条既有/上游弃用提示；记录 `artifacts/lifecycle-full-regression-20261023.xml` |
| 最终定向验证 | 69 passed，4.96 秒；覆盖完整回归启动后追加的单位检查、目标字段隔离及交叉场景；记录 `artifacts/lifecycle-final-focused-20261023-004.xml`，不累加重叠批次 |
| 账户事件 | 登记不提前计收益、除权应收不可花费、支付不重复计收益、声明扣税、拆股/交收、残股拒绝、终止减记及延迟结算、缺价估值与未来事件扰动 |
| CLI 与冻结重算 | `target-lifecycle-fixture-20261023-003` → `target-lifecycle-result-20261023-003`，`target-verify --inputs` PASS，结果 SYNTHETIC_ONLY |
| 数值参考 | 当前源码 `e2e-m0-windows-lifecycle-20261023-001/acceptance.json` PASS，手算账户与优化层梯度通过 |
| 发布边界 | 231 个发布文件扫描无错误；六份文档 43 个本地链接无断链；新源码导出目录 `artifacts/releases/20261023-lifecycle-source/`，验收记录 `artifacts/lifecycle-release-verification-20261023.json` |

本轮没有真实市场重训、服务器部署或外部复现。仅支持明确声明的事件/扣税与整数股数；跨日挂单、未知事件、真实持有期税制和非整数拆股不作为已支持能力。共享组合层、连续效用训练及冻结经济比较继续按 SPEC 推进。

## 2026-10-23 共享组合层与训练规格

实现及命令见 [共享组合层](PREDICTION_ALLOCATION.md)，生产映射与兼容边界见 [ADR 005](specs/ADR_005_PREDICTION_ALLOCATION.md)。主 SPEC 更新至 v0.4，后续以 [N03 连续效用训练 SPEC](specs/N03_UTILITY_TRAINING_SPEC.md) 为实施与验收入口。

| 检查 | 结果与范围 |
| --- | --- |
| 最终完整回归 | 干净 Windows CPU 环境 380 passed，0 失败/跳过，620.47 秒；137 条既有 Starlette/httpx 与 CVXPYlayers/NumPy 弃用提示；`artifacts/allocation-full-regression-20261023.xml` |
| 数值与会计 | `artifacts/e2e-m0-windows-allocation-20261023-001/acceptance.json` PASS，闭式解误差约 2.27e-14，原始求解残差约 2.78e-11，三个有限差分步长通过；手算账户账本残差为零 |
| 组合因果性与状态 | 风险/成本未来扰动不变、可见修订、历史不足失败、真实漂移持仓、退出池持仓、交收和应收预算 |
| 生产与归档 | 显式有界投影及原始解保留、源数据/代码变更拒绝、取消台账、重哈希篡改检测、CLI 与全量风险/成本/账户重算 |
| 合成共同账户 | `artifacts/allocation-demo-20261023-004/`，B0/B1 各 29 个目标，0 失败，完整重算；两者实际路径相同，无模型优势结论 |
| 费用差异 | 实际 250.0758 元，成交代理 68.3677 元，意图代理几乎为零；相对费用差约 265.78%，按权益阈值未超限不能解释为代理准确 |
| 依赖与源码扫描 | `uv pip check` 通过，60 个包；240 个发布文件扫描，0 错误；diff 空白检查通过，主要文档本地链接无断链 |

完整回归覆盖最终 Python/平台环境身份及汇总诊断修改；不累加之前的 62 项针对性批次。源码交付目录为 `artifacts/releases/20261023-allocation-source/`，发布校验记录 `artifacts/allocation-release-verification-20261023.json` 包含 ZIP CRC/精确清单、源码/导出/ZIP 内容哈希、导出安装导入位置、CLI 回放及原始输入重算、开发环境恢复。源包不含真实数据/权重/虚拟环境。

N03 尚未实现。其前置门槛已明确要求解释漂移、动作到成交及成本误差，声明 HiGHS 生产投影不反传，并分别验证固定动作与账户状态反馈路径。没有真实市场重训、新经济确认、Linux 实测、服务器部署或非实现者外部复现；这些门槛保持未完成。

## 2026-10-23 连续账户和受控梯度

此节接续上一检查点，实现范围见 [ADR 006](specs/ADR_006_CONTINUOUS_ACCOUNT.md) 和 [连续账户说明](CONTINUOUS_ACCOUNT.md)。N03 只有账户/效用基础及固定动作诊断，完整训练阶段仍未验收。

| 检查 | 结果与范围 |
| --- | --- |
| 最终定向回归 | 86 passed，67.49 秒，178 条上游 CVXPYlayers/NumPy 弃用提示；`artifacts/continuous-final-focused-20261023-001.xml`；包括新连续账户、共享组合层、数值优化、生命周期和独立目标 Bench |
| 账户与事件 | 手算隔夜/开盘/日内/费用、含费用的资金上限、最低佣金、限价/停牌/容量/缺价、整数手、交收、应收不可花费、登记后卖出/延迟支付、拆股批次与终止索赔 |
| 计算图 | 资金约束到先前现金、资本到因果成本系数、状态反馈到多日效用、CVXPYlayers 参数到多日效用的受控有限差分；分红权利保留梯度；整数取整导数为零 |
| 冻结诊断 | B0 `artifacts/continuous-audit-20261023-002/` 与 B1 `artifacts/continuous-gru-audit-20261023-001/`，各八种执行/费用模式；原账户重放及原决策状态的因果配方重算通过 |
| 整数完整模式 | 数量、锁定数量及 UTC 交收日程差异为零，权益/现金最大差异约 8.15e-10 元，费用约 8.88e-16 元，应收为零差异 |
| 同成交费用分解 | 最低佣金溢价 192.746076 元，代理额外冲击 11.037924 元，税率/滑点差异为零；相抵为 181.708152 元费用模型差 |
| 明确负结果 | 分数份额最低佣金模式 B0 费用约 528.47 元、B1 约 508.47 元，整数完整模式均约 250.08 元；代理与生产动作的边界偏差仍存在，不以软件恒等检查通过当作准确性验收 |
| 发布与来源 | 248 个发布文件扫描无错误；新源码包 `artifacts/releases/20261023-continuous-source/`，ZIP/清单/内容哈希与导出安装运行记录 `artifacts/continuous-release-verification-20261023.json` |

首批 21 项连续账户测试有 6 项因测试修改协议后沿用旧提交身份而拒绝；改为在新协议下重新绑定真实状态生成目标后通过。最终 86 项覆盖修正后的全部场景；中间的 22/26 项与本批次重叠，不相加。未重跑整套主项目回归，上一 N02 检查点的 380 项完整回归不代表本次新增模块也参加过该批次。

没有完整 GRU 效用训练、截断/恢复、生产映射梯度或闭环模型比较的验收结论。源码与诊断均保留原身份，历史结果不回写。真实数据、Linux、服务器部署及外部复现门槛未改变。

## 2026-10-23 GRU 净效用训练检查点

接续上节，增加本地效用训练 API 与合成 smoke；完整 N03 尚未验收，当前边界见 [训练说明](UTILITY_TRAINING.md)。

| 检查 | 结果与范围 |
| --- | --- |
| 最终定向回归 | 117 passed，98.75 秒，280 条上游弃用提示；`artifacts/decision-final-focused-20261023-004.xml`；覆盖新增 31 项及连续账户、共享组合、优化、生命周期和独立目标 Bench |
| 生产映射 | 箱体/预算/换手、零预算/零 slack/零换手、不可用锚点、超量修正/非法输入拒绝，各光滑分支差分；严格精确目标、实际状态与预算梯度 |
| 真实计算图 | GRU 参数经求解/生产映射/跨日账户到效用的方向导数；状态截断前向一致，同时复现并阻止旧分母伪梯度 |
| 训练和来源 | 源归档/配方/seed/权重拒绝、可用修订截止、冻结 encoder、无梯度验证、合法零梯度、失败/取消、episode 预算；强制最佳 epoch 早于最后一轮，核对同轮权重及优化器步数 |
| 最终合成 smoke | `artifacts/utility-smoke-20261023-002/`：源 GRU fold 0/seed 0，两轮，选 epoch 2，参数变化范数 0.03959895；同精度 B1 控制和 B2 的三个反馈/固定动作路径，独立目标提交重算通过 |
| 负结果 | 三日精确收益 B1 -0.03130%、B2 -0.03468%；B2 连续费用 323.0552 元 vs 精确 36.0843 元；成本相对差约 795.28%，主要比较资格为 false，不作模型优势/市场结论 |
| 内存 | 首次 smoke 进程高水位 439738368 字节，包含导入及此前 trial；状态截断不保证内存随 K 有界 |
| 源码交付 | 检查点 `artifacts/releases/20261023-utility-source/`；源码/导出/ZIP 清单、导出安装及重跑记录见 `artifacts/utility-release-verification-20261023.json`，以实际状态为准 |

当前实现身份 `sha256:d77c7643568d71f4484c653c2ec6d617a81b2b428ea7ac146642ea52c80bb089`。最初测试夹具遗漏 checkpoint 的 history 后已修正；最终批次覆盖该路径。未重跑完整主项目测试，旧 N02 的 380 项不能视为包含本次新增模块；重叠批次不相加。完整配置/worker、生产映射消融、完整代理差异门槛、Linux、外部复现和实际部署继续待完成。

## 2026-10-23 效用训练共享运行检查点

此节接续上节，源码身份为 `sha256:c1b4d65083fe74266a4712a9e057648360f8eb354d13166af7813456780fc19b`；说明见 [效用训练](UTILITY_TRAINING.md)，规范为 [主 SPEC v0.6](specs/E2E_RESEARCH_PLATFORM_SPEC.md) 和 [N03 SPEC v0.4](specs/N03_UTILITY_TRAINING_SPEC.md)。历史结果保留原身份。

| 检查 | 结果与范围 |
| --- | --- |
| 当前源码完整回归 | 454 passed，0 失败/跳过，701.35 秒；369 条既有 Starlette/httpx 和 CVXPYlayers/NumPy 弃用提示；`artifacts/utility-full-regression-20261023-001.xml`，覆盖本轮共享配置、CLI/worker 及新增效用模块；不与定向批次相加 |
| 最终定向验证 | 16 passed，32.79 秒，88 条上游弃用提示；`artifacts/utility-integration-20261023-002.xml`；11 项运行集成场景及 5 项极小目标规则场景，不累加重叠批次 |
| 正式 worker 合成训练 | `artifacts/utility-pipeline-demo-20261023-001/`，两轮、选 epoch 1，归档 `runs/history/f68baf4a75af4d2f961e48040463aa98/`，完整 trial、控制/微调 checkpoint 及三条评价路径 |
| 扩展 CLI 验证 | `qwb e2e utility-verify <上述归档> --inputs` PASS；配置/数据/原协议/源归档身份、模型动作、辅助预测与标签、独立账户算术重算；不是重训模型 |
| 数值参考 | `artifacts/e2e-m0-windows-utility-pipeline-20261023-001/acceptance.json` PASS；闭式解误差约 2.27e-14，原始求解残差约 2.78e-11，三个光滑差分步长通过，手算账户残差为零；不替代完整 N03 |
| 明确负结果 | 精确净收益 B1/B2 约 -0.03130%/-0.03471%；B2 连续/精确费用约 67.27/39.82 元，费用、数量、换手及动作等超限；两组比较资格 false，无模型优势结论 |
| 数值规则边界 | 新 ADR 008 规则抑制显式极小目标，不删除现有持仓或费用；新规则和训练共同改变，费用下降没有孤立归因，固定原始解/固定 checkpoint 消融待完成 |
| 环境与文档 | `uv pip check`：60 个包兼容；262 个可发布文件扫描 0 错误；15 份主要 Markdown 的 126 个本地链接无断链；diff 空白检查通过 |

归档完整性、策略/账户可重算和代理一致性是不同检查。应收目前比较总额，权利/支付日程及已处理事件集合尚无独立资格门槛；零费用分母与重哈希辅助预测篡改的专门回归、生产映射消融及完整阶段评审继续待完成。Linux 实测、非实现者复现、真实市场经济确认和服务器部署没有新增完成结论。此前 `20261023-utility-source` 源码包早于本轮变更，不能代表当前实现。

本轮源码交付目录为 `artifacts/releases/20261023-utility-pipeline-source-001/`，清单见导出内的 `SOURCE_MANIFEST.json`；导出核验结果另记 `artifacts/utility-pipeline-source-verification-20261023-001.json`。该导出只验证清单、内容身份和 ZIP 可读性，没有在导出目录重新安装或重训，不能沿用旧包的安装验收结论。Git 推送和服务器部署未执行。

## 2026-10-23 权利日程与固定模型消融

当前源码身份 `sha256:46cc891cf26c6de7555530559c6c0b6f08d2d071c0315e2b7e5bd98e2d4eed58`，规范见 [N03 SPEC v0.5](specs/N03_UTILITY_TRAINING_SPEC.md) 和 [ADR 009](specs/ADR_009_LIFECYCLE_COMPARISON.md)。

| 检查 | 结果与范围 |
| --- | --- |
| 当前源码完整回归 | 474 passed，0 失败/跳过，585.03 秒，449 条既有/上游弃用提示；`artifacts/utility-state-full-regression-20261023-001.xml`，包含新增比较、生产消融和共享运行测试；不与定向批次累加 |
| 定向回归 | 94 passed，109.57 秒，312 条上游弃用提示；`artifacts/utility-state-focused-20261023-002.xml`，与完整回归不相加 |
| 真实账户场景 | 决策前/收盘后权利日程、同总额但支付日/事件/资产/类型/股数/已处理事件不同、跨事件金额相抵、等价时区、未确认/零金额权利与零持仓事件 |
| 费用和预测篡改 | 精确费用为零时相对差 null、绝对门槛仍生效且等号可通过；辅助预测与 seed 预测修改并重算全部哈希后，普通归档校验成功而扩展重算拒绝 |
| 正式训练与重算 | `artifacts/utility-pipeline-demo-20261023-002/`，归档 `626893c62a574d73ab56faf0b97389fd`，两轮选 epoch 1，比较版本 `lifecycle_schedule_v1`，扩展验证 PASS；代理资格 false |
| 冻结原始解 | `artifacts/utility-production-ablation-20261023-001/`，12 份相同原始解；strict_raw 接受 0、HiGHS/旧 Tensor/新 Tensor 各接受 12；结构检查与实际账户绑定分开 |
| 固定 checkpoint | 两个 checkpoint × 两种 Tensor × 三条账户路径；四组均完成且代理超限，模型各自两规则的精确收益/费用相同；B2 连续费用 277.2737 → 67.2736 元，B1 346.3199 → 156.3201 元 |
| 数值参考 | `artifacts/e2e-m0-windows-utility-state-20261023-001/acceptance.json` PASS，当前源码的手算账户与光滑求解/梯度检查；不是整个 N03 验收 |
| 源码与文档 | 267 个可发布文件扫描 0 错误；16 份主要 Markdown 的 137 个本地链接无断链；diff 空白检查通过 |

源码导出目录为 `artifacts/releases/20261023-utility-state-source-001/`，最终清单、独立本机环境安装及同配置训练重跑以 `artifacts/utility-state-release-verification-20261023-001.json` 的实际结果为准。此环境使用本机已有构建 wheel 缓存，不等同于未配置编译工具的新机器或非实现者复现。N03 完整阶段评审、代理超限的剩余执行差异、N04 公平经济比较、Linux/GPU、外部复现及服务器部署均未关闭。

## 2026-10-23 R01 执行归因与完整诊断重算

本轮新增诊断脚本及测试，未修改 `src/qwb` 的 Python/YAML；核心实现身份仍为上节 `46cc891c...eed58`。脚本 `scripts/run_utility_execution_audit.py` 的 SHA256 为 `02ee508d6f4b9926162078d273bd045680e6a0131b099629f15f11e29f5e76f7`，与核心实现身份分别核验。说明见 [效用训练](UTILITY_TRAINING.md)，要求见 [接续 SPEC R01](specs/NEXT_ITERATION_SPEC.md)。

| 检查 | 结果与范围 |
| --- | --- |
| 最终定向回归 | 53 passed，0 失败/跳过，47.94 秒，42 条上游弃用提示；`artifacts/utility-execution-focused-20261023-003.xml`；9 项新诊断测试及原连续账户/生命周期比较/生产规则场景，不与之前 46/9 项批次累加 |
| 固定模型运行 | `artifacts/utility-execution-audit-20261023-002/`，B1/B2 各七种连续场景及两条精确参考，全部完成；没有训练或选择模型 |
| 整数参考 | 固定目标下股数/锁定股数/未成交差为零；B1/B2 最大权益差约 2.33e-10/1.16e-10 元；原资格阈值未更改 |
| 差异桥接 | 逐日权益与隔夜/日内/事件盈亏、成本桥接可重算；整数手 × 最低佣金路径交互单独报告；不能将顺序差额解释为唯一因果贡献 |
| 压力与失败 | 交收、停牌、资金及最低费实际影响成交；反事实失败保留、覆盖不齐不输出完整桥接；逐资产/买卖方向匹配，重复记录和盈亏残差拒绝 |
| 篡改与完整重算 | 修改诊断结果并重哈希仍被算术比较拒绝；正式诊断从原 checkpoint/数据重新运行三路径与全部机制并逐文件比较 PASS，`artifacts/utility-execution-verification-20261023-002.json`；不是重训 |

本机两模型整数模式在这些场景与独立固定动作一致，不证明分数训练代理准确。B2 分数费用 67.273597 元、独立固定动作费用 38.593629 元、精确自身状态闭环费用 39.821832 元；分数代理仍超限，源运行的经济确认和比较资格未提升。正式脚本前的 001 诊断保留原脚本身份，旧源码 ZIP 没有本轮脚本，未被覆盖。

本轮没有重跑完整主项目回归；上节 474 项完整检查仅覆盖其原范围，不作为新脚本参加过该批次的证据。未新增真实数据训练、Linux/GPU、非实现者复现或部署结论。R02 验证排序审查、R03 阶段评审及 N04/N05 保持未完成。

## 2026-10-23 R02 候选验证与选择隔离

当前实现身份 `sha256:5c8457692e64f3f3633fdb61dfe797f15cc0c41e24a405533f5d7a0d79730462`，对应 [ADR 010](specs/ADR_010_VALIDATION_CANDIDATE_AUDIT.md)、[N03 SPEC v0.6](specs/N03_UTILITY_TRAINING_SPEC.md) 与 [接续 SPEC v1.1](specs/NEXT_ITERATION_SPEC.md)。R02 的本机 Windows CPU 小型合成软件范围已验收；完整 N03 与经济资格不因此通过。

| 检查 | 实际结果与范围 |
| --- | --- |
| 完整主项目回归 | `artifacts/validation-candidate-full-regression-20261023-001.xml`：495 passed，0 失败/错误/跳过，831.52 秒，825 条既有 Starlette/httpx 与 CVXPYlayers/NumPy 弃用提示；包括 R01 诊断和 R02 配置/共享运行等本次收集到的测试 |
| 收集后独立追加 | `artifacts/validation-candidate-earlystop-20261023-004.xml`：1 passed，控制台 25.40 秒；真实零梯度平分触发早停、not_run 保留及缺失候选拒绝。不合称为一次 496 项完整回归 |
| 排序与失败 | 已知反转/平分/未定义 tau、与 SciPy 核对、失败不只汇总成功子集、候选审查与训练状态分离；取消/超时中止原 trial，审查计入原预算 |
| 训练隔离 | 启用/关闭审查的实际模型历史、已选参数与 CPU RNG 相同；冻结 encoder、逐轮优化器与已选同轮状态核验；更改外层 test 停牌条件不改变候选或已选模型 |
| 完整重算 | 修改候选结果并重算归档哈希后，普通库存完整性可通过，扩展算术重算仍拒绝；每个候选连续损失须与原该轮验证完全一致 |
| Windows 原子写入 | 初次集成 24 passed / 1 failed，深层临时文件名过长；改为短且独占的同目录临时文件后，路径/外层隔离最终定向 2 passed，53.03 秒，`artifacts/validation-candidate-path-20261023-003.xml`，并由上述完整回归覆盖；不累加重叠批次 |
| 正式 worker | `artifacts/utility-validation-demo-20261023-002/runs/history/c4a3696edcdb4479b632a0122ccc9195/`：110 日/24 资产合成源，单折/seed 0，三轮完整候选，原选 epoch 3，扩展重算 PASS，`validation_audit_complete=true` |
| 排序风险 | 连续 3/1/2，精确 1/2/3，精确 2/3 平分；1/3 反转，Kendall tau-b=0。审查不改原选模；不从这个已查看案例制定新选模后再冒充独立确认 |
| 原资格 | B1/B2 精确净收益 -0.000312976584/-0.000346827839，精确费用约 72.748224/36.084320 元；代理门槛仍超限，`comparison_eligible=false`、`SYNTHETIC_ONLY`、优势未建立 |
| 当前 CPU 数值 | `artifacts/e2e-m0-windows-validation-candidate-20261023-001/acceptance.json` PASS；手算残差零、闭式参考误差约 2.27e-14、原始求解残差约 2.78e-11、三个光滑差分步长通过；不是全部 N03 条款 |

完整测试命令：

```powershell
.\artifacts\environments\e2e-clean\Scripts\python.exe -m pytest -q --junitxml=artifacts/validation-candidate-full-regression-20261023-001.xml
.\artifacts\environments\e2e-clean\Scripts\python.exe -m pytest -q tests/test_validation_audit.py::test_early_stopping_retains_unrun_candidates_and_missing_epoch_is_rejected --junitxml=artifacts/validation-candidate-earlystop-20261023-004.xml
```

源码交付目录 `artifacts/releases/20261023-validation-candidate-source-001/`，ZIP 内以 `SOURCE_MANIFEST.json` 固定全部源码。安装与三轮候选重训使用新的 `artifacts/environments/validation-export-20261023-001/`，不重装主 `e2e-clean` 环境；结果另存 `artifacts/validation-candidate-release-verification-20261023-001.json`，以实际 status 为准。该核验同时检查 ZIP CRC/精确库存、工作区/导出/ZIP 文件哈希、导出代码导入路径、锁依赖、全部候选与已选参数/优化器/CPU RNG、确定性训练记录、内层指标/排序、辅助预测和独立账户。耗时/进程高水位与运行模型身份单独排除，不能把它们称为全部文件逐字节相同。

复现只限同机、缓存依赖、一个合成 fold/seed，并读取工作区已有初始化和数据；不是可携带的公开数据包、冷机器或非实现者证明。当前导出不提升 Linux/GPU 或服务器部署状态。历史 `utility-validation-demo-20261023-001` 属于 `7f132250...20f7c` 身份；旧 R01 归档仍属 `46cc891c...eed58`，不回写为本轮身份。

阶段差距见 [N03 阶段审查](specs/N03_STAGE_AUDIT_20261023.md)。N03 原生失败归档/联合因果扰动/完整边界/资源规模与 N04/N05 仍开放；代理排序反转是要保留的负证据，不是模型训练无条件可靠的证明。

## 2026-10-23 R03 原生链路、失败与费用单位

本轮核心身份 `sha256:73cb717fe7effe4b1095cd053b9baf3591a895656767aac7c22fe7e1d2769c9e`，规范见 [主 SPEC v0.9](specs/E2E_RESEARCH_PLATFORM_SPEC.md)、[N03 SPEC v0.7](specs/N03_UTILITY_TRAINING_SPEC.md) 与 [接续 v1.2](specs/NEXT_ITERATION_SPEC.md)。变更增加诊断和费用明细版本，不改变原成功动作、训练目标、连续选模或代理门槛。

| 检查 | 实际结果和范围 |
| --- | --- |
| 当前完整主项目回归 | `artifacts/decision-integrity-full-regression-20261023-006.xml`：525 passed，0 失败/错误/跳过，890.50 秒，1179 条既有 Starlette/httpx 与 CVXPYlayers/NumPy 弃用提示；含 22 项原生链路及 7 项成本分解测试，不与定向或历史批次累加 |
| 失败上下文最终定向 | `artifacts/decision-integrity-failures-20261023-005.xml`：8 passed，14 deselected，46.65 秒，132 条 CVXPYlayers/NumPy 弃用提示；缺历史、真实后端失败、真实生产映射拒绝和真实反传非有限梯度，均覆盖首轮及完成一轮后失败 |
| 归档断言 | 保留原异常、epoch/phase/completed_epochs 和决策身份/约束/配方；生产拒绝保留原始分配及原始求解审计；已完成候选保留，第二折 not_run，无已选模型、成功 metrics 或 worker 输出指针 |
| CPU 数值 | `artifacts/e2e-m0-windows-decision-integrity-20261023-001/acceptance.json` PASS；手算会计残差 0，闭式参考误差约 2.27e-14，原始求解残差约 2.78e-11，三个光滑差分步长通过；不是全部 N03 验收 |
| 原 N02 固定目标当前重放 | `artifacts/continuous-r03-replay-20261023-001/` COMPLETED，八种机制全部完成；原独立账户 MATCH、原因果配方 RECOMPUTED；不改写历史源身份，不重训编码器或重跑旧优化器 |
| 原 N02 整数完整参考 | 最大权益/现金差约 8.15e-10 元，数量/锁定数量/交收日程差 0，成本差约 8.88e-16 元；实际费用 250.075836 元，同成交代理 68.367685 元，最低佣金溢价 192.746076 元、负代理冲击项 -11.037924 元，总残差约 4.44e-15 元 |
| 当前正式 worker | `artifacts/utility-integrity-demo-20261023-001/runs/history/32a942d01fcd4ee0ae6978056fcc35dc/`；110 日/24 资产合成源，单折/seed 0，三轮候选及选中模型扩展重算 PASS；选 epoch 3，连续 3/1/2、精确 1/2/3（2/3 平分），tau-b=0，全部审查完成 |
| 当前精确外层结果 | B1/B2 净收益 -0.000312976584/-0.000346827839，费用 72.748224/36.084320 元；两组代理门槛仍超限，comparison_eligible=false、SYNTHETIC_ONLY；没有市场优势结论 |
| 费用 v2 正式诊断 | `artifacts/utility-integrity-execution-20261023-001/`；两固定 checkpoint，各七机制与两精确路径全部完成，逐笔与桥接均含单位；精确自身状态 B1/B2 分别 22/9 笔、费用 0.727482240/0.360843200 bp（固定期初权益均 100 万元），成本分解总残差 0/约 8.88e-16 元 |
| 当前诊断完整重算 | `artifacts/utility-integrity-execution-verification-20261023-001.json` PASS；从原 checkpoint/源数据重算全部机制与确定性产物，不是重训；脚本哈希 `sha256:367e85b614ae37dc55519828a60d41e652ea5bad55ef049643e038994bc34481`，旧诊断必须用原脚本身份核验 |

开发失败保留：早期 `decision-integrity-focused-20261023-001.xml` 是事件时点/折日期夹具问题（8 failed/1 passed）；修正后 `...002.xml` 9 passed；费用/因果/周边回归 `...003.xml` 123 passed（220.67 秒、804 条上游提示），早于失败上下文改动，不当作最终当前核心完整回归。新增失败上下文初次 `decision-integrity-failures-20261023-004.xml` 8 failed，原因是诊断错误读取连续状态不存在的 `data_snapshot_id`；改用已核验 bundle 快照身份后由 `...005.xml` 通过。没有通过降低断言或放松协议绕过错误，各批次不累加。

费用 API 默认 v1 保持兼容，执行诊断显式采用 `same_fill_cost_decomposition_v2`。逐笔/逐日/总计报告 CNY、固定第一条审计期初权益 bp，逐笔另报成交额 bp；相对差的分母为同成交代理，零分母为 null，绝对差和会计残差保留。手算用例覆盖买卖费税/滑点/冲击/最低费、变化权益固定分母、零成交额和零代理、篡改及版本拒绝。

G0-06 的原生评价逐日失败续持、资源规模、完整阶段签结及 N04/N05 仍开放。meta 设备拒绝仅验证显式非 CPU 拒绝，不是 GPU 实测；本轮继续是本机 Windows CPU 合成软件证据，未新增市场优势、Linux、非实现者复现或服务器部署结论。

正式 worker 运行中 Windows 个别进度事件因追加文件被占用，在五次重试后转 stderr；这是既有日志降级路径，不能声称 events.jsonl 保存了每条进度。trial/history/candidate/checkpoint/账户归档均已写入并通过重算；本轮未改日志实现，后续运维需补持久化降级和监控。正式运行与完整回归有短时并行，不把耗时或进程内存当成独立资源规模测试。

当前检查与运行命令：

```powershell
.\artifacts\environments\e2e-clean\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts/decision-integrity-full-regression-20261023-006.xml
.\artifacts\environments\e2e-clean\Scripts\python.exe -m qwb.cli e2e check --output artifacts/e2e-m0-windows-decision-integrity-new
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_continuous_audit.py artifacts/allocation-demo-20261023-004/B0-ridge --output artifacts/continuous-r03-replay-new
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_utility_pipeline_demo.py --source-run artifacts/sequence-demo-20261023-003/runs/history/a70e812b2ae7454483ee84d023e22e88 --bundle artifacts/sequence-demo-20261023-003/fixture --output artifacts/utility-integrity-demo-new --epochs 3 --validation-audit
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_utility_execution_audit.py --utility-run artifacts/utility-integrity-demo-20261023-001/runs/history/32a942d01fcd4ee0ae6978056fcc35dc --output artifacts/utility-integrity-execution-new
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_utility_execution_audit.py --verify artifacts/utility-integrity-execution-new
```

输出目录使用新名称。上述命令需要工作区已有且内容身份匹配的合成初始化/数据/原 N02 产物，不是首次 GitHub 下载者自带的完整公开证据包。当前源码导出为 `artifacts/releases/20261023-decision-integrity-source-001/`，库存/ZIP 核验另存 `artifacts/decision-integrity-source-verification-20261023-001.json`；该批次仅核验内容一致/CRC。随后安装重训的新增证据见下一节，不能从 R02 包迁移结论；未推送 GitHub 或执行实际部署。

## 2026-10-23 R03 当前四映射与导出重训

核心身份仍为 `sha256:73cb717fe7effe4b1095cd053b9baf3591a895656767aac7c22fe7e1d2769c9e`，没有更改训练、生产映射或原选模规则。

| 检查 | 结果与范围 |
| --- | --- |
| 当前四映射冻结解重放 | `artifacts/utility-r03-production-20261023-001/` COMPLETED；12 份相同原始解，strict_raw 接受 0/12，HiGHS/两 Tensor 各 12/12；拒绝与结构可行性不等于实际账户可成交 |
| 当前固定模型闭环 | B1/B2 各两种 Tensor、各三条账户路径均完成；B1 连续费用 346.3199 → 156.3201 元，B2 268.0552 → 63.0552 元；两模型各自精确费用/收益均不变，四组代理资格 false；不是重训、改选或模型增益 |
| 冻结源码包安装 | `artifacts/utility-r03-export-verification-20261023-001.json` PASS；276 文件的 workspace/stage/ZIP 内容全部一致，ZIP CRC PASS，SHA256 `3a51131d56e278d1ac5610713c1e4f86ce41b1dff3abab8008473a805aebf8c5` |
| 独立环境 | `artifacts/environments/decision-integrity-export-20261023-001/`，锁定 CPU 依赖从同机缓存安装，editable 导入确认来自导出目录，依赖检查通过；没有修改原研究环境 |
| 全部候选重训 | `artifacts/utility-r03-export-20261023-001/runs/history/312e58080ae940f48f477bbac55cdc11/`；三轮、所有候选及所选参数逐 Tensor 一致，优化器/CPU RNG 一致，排除耗时/RSS 后确定性记录一致；预测、候选连续/精确指标及独立账户一致 |
| 原选择与资格 | 仍选 epoch 3；连续顺序 3/1/2、精确 1/2/3（2/3 平分）、tau-b=0；两组资格仍 false，不因复现通过升级 |

这是同机 Windows CPU 缓存依赖下的合成重训；源初始化与数据仍取自工作区，不是冷机器、自包含公开包、Linux/GPU、非实现者复现或服务器部署证据。此冻结 276 文件包不含随后添加的资源诊断脚本、测试和说明；不能将安装结论扩展到这些新文件。完整主项目 525 项结果仍指上一节批次，此处没有重新运行或累加测试。

## 2026-10-23 R03-R 独立进程资源矩阵

`src/qwb` 核心身份仍为 `73cb717f...69c9e`；仅新增资源脚本、测试与说明，脚本身份 `sha256:053626a8c223e7c57422003e2e00daaeb2713000719d853d27e2776ced1cb6e3`。完整测量口径和十二组中位数见 [资源说明](UTILITY_RESOURCE_AUDIT.md)。本节不新增完整主项目回归结论。

| 检查 | 实际结果与范围 |
| --- | --- |
| 新脚本最终定向批次 | `artifacts/utility-resource-focused-20261023-003.xml`，17 passed，0 失败/错误/跳过，控制台 88.14 秒；真实预训练、重复 fit/重载、训练预算失败、源失败保留 not_run、真实子进程及 Windows 嵌套解释器树超时清理、重哈希改汇总/删场景拒绝；不与核心 525 项累加 |
| 过程中的失败与修复 | 001 为 12 passed/2 failed，真实训练通过而协议比较失败：Python tuple 与归档 JSON list 原始相等比较不同；改为规范 JSON 身份比较，002 为 15 passed，003 最终增加更强溯源与真实 fit 超时检查后 17 passed；旧批次不覆盖或累加 |
| 冻结矩阵 | `artifacts/utility-resource-audit-20261023-001/`，资产数 24/64/128 × train 3/12 步 × detach full/2 × 两次重复；一轮、seed 0、CPU/依赖单线程，fit 120 秒、子进程墙钟 180 秒 |
| 真实来源与完整分母 | 三个资产池各由共享 pipeline 实际预训练一轮，源/数据/cutoff 经核验；三个准备成功，效用 fit 24/24 完成，失败/超时/not_run 均 0；不以此推断普遍失败率为零 |
| 隔离与重复性 | 预训练/fit 各在新子进程顺序执行，无并行训练/测试/安装/其他基准；已有站点/UI 服务保留运行，不宣称机器独占。12 组内重复参数逐 Tensor 相同，保存重载一致 |
| 实测时间/RSS | 全体 fit 9.454–43.782 秒，父进程墙钟 13.438–49.563 秒，最终进程 RSS 高水位 418205696–798928896 字节；包括导入/加载，不能减去 fit 前高水位当训练增量内存 |
| 截断对照 | 128 资产/12 步 full/2 的中位 RSS 分别约 759.9/761.8 MiB；本次截断组未更低，不声称节省内存或随 K 有界，也不据两次重复推断普遍效应 |
| 学习信号限制 | 24 资产梯度范数约 `7.75e-4`–`1.52e-2`；64 资产约 `1.15e-12`–`4.08e-12`、128 资产约 `2.71e-13`–`1.16e-11`，后两组更新很小；运行成功不证明有效学习，后续 R04-D 在开发数据诊断，不追溯改源/配方 |
| 溯源与汇总核验 | `artifacts/utility-resource-verification-20261023-001.json` PASS；真实源、全部声明格、task/协议/输入/进程/线程/checkpoint、重复参数及汇总核验；不是重训或计时认证 |

intent SHA256 `b55628c218ebb564b53232a155b1ffb39cedce8e976eaf509d159f75b283154e`；产物库存 SHA256 `b5514c477eb897b508573917539a08eabb48dd9965509bcf601200b485c7c941`。资源原始产物与本地验证 helper 位于被忽略的 artifacts；不自动随 GitHub 源码发布。新来源可由下述工具重新生成，不能因此把已有本地产物称为外部或公开研究复现。

```powershell
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_utility_resource_audit.py --output artifacts/utility-resource-new --assets 24 64 128 --steps 3 12 --detach full 2 --repeats 2 --epochs 1 --max-seconds 120 --wall-seconds 180
.\artifacts\environments\e2e-clean\Scripts\python.exe scripts/run_utility_resource_audit.py --verify artifacts/utility-resource-new
```

本轮源码交付目录 `artifacts/releases/20261023-resource-audit-source-001/`，核验报告 `artifacts/utility-resource-source-verification-20261023-001.json` 记录实际清单、workspace/stage/ZIP 内容和 CRC 状态。新包含资源脚本/测试/说明及主 SPEC v0.10、N03 v0.8、接续 v1.3；只作源码库存核验，不继承旧 276 文件包的独立安装/重训结论。没有推送 GitHub 或实际部署。

R03-R 在冻结测量范围内已有证据；R03-F 的原生精确诊断失败续持、R03-S 最终逐条签结、代理资格、N04/N05 仍未关闭。SPEC C4-F 只是后续协议要求，不是已开放的运行参数；本轮软件、资源与同机复现通过均不建立模型优势或市场效用。

## 2026-10-23 R03-F 外层失败续持

本轮核心身份 `sha256:86eb170d21c70654a2b2e5985209dc1c4f9ce0fa6def695e55f16cd46c088f94`。按 [ADR 011](specs/ADR_011_OUTER_EVALUATION_FAILURES.md)、主 SPEC v0.11 / N03 v0.9 / 接续 v1.4 实施可选 `outer_evaluation`。上节 `73cb717f...69c9e` 的 525 项回归、资源矩阵及导出安装重训属于历史核心，不能据此声称本轮重新测量或重训过。

最终定向批次 `artifacts/outer-evaluation-focused-20261023-005.xml`：105 passed，0 失败/错误/跳过，374.51 秒，1120 条上游 CVXPYlayers/NumPy 弃用提示。覆盖五类原生策略失败 × 首日/成功后/连续失败恢复，受限持仓与公司行为、连续中止和 not_run、合法零成交、系统/程序/账户错误中止、严格配置、实际模型参数/标签缩放身份、共享 worker 全失败的空预测、训练仍中止，以及整份运行归档重新哈希后的覆盖/上下文/提交/汇总/trial 篡改拒绝。固定模型归因/消融在缺失连续评价时明确拒绝。不同批次不相加，完整回归另记。

开发过程保留早期失败产物：001 将收盘时间误作晚于收盘发布的反馈可用时点，已改为显式 `feedback_cutoff`；002 的拆股夹具除权价把每原股分红当成每新股分红，修正夹具并保留严格会计检查；003 的合法零成交可能存在数量为 0 的拒单记录，改为核验目标成功、数量与费用为 0。004 为修补实际参数身份和整档重哈希前的 90 项通过批次，不代替最终 005。正式样例、数值入口及最终完整回归按实际结果继续入账。

正式自包含样例命令：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe scripts/run_utility_failure_demo.py --output artifacts/utility-failure-demo-20261023-001
```

`result.json` 为 COMPLETED，`verification.json` 为 PASS。先冻结意图，再生成 110 日/24 资产/seed 17 的合成源，在真实预测预训练前将一个资产 calendar[87] 的 high 置缺失；源 GRU 一轮，效用两轮，候选审查完整、原规则选 epoch 1。没有运行时故障注入、手改收益头或复用以前的初始化。源运行 `source/runs/history/5e7b220dd29342adbd1a0681c7c7a5d0/`，效用归档 `utility/runs/history/41b245d758824591852dd0c8a3e22377/`。

B1/B2 各声明 3 个精确日期，均为 target / explicit_failure / target；每模型 2 成功、1 失败。两模型失败日均为 0 笔成交、0 费用、股数变动为 0，恢复日使用推进后的账户。连续均为 1 completed / 1 failed / 1 not_run，完整连续收益 null、三路径比较 INCOMPLETE。聚合精确失败 2、连续失败 2，外层为 DIAGNOSTIC_WITH_FAILURES，worker 和训练归档 succeeded，比较资格 false。B1/B2 精确净收益约 -0.036875% / -0.039451%，费用约 51.5669 / 29.3245 元，不构成效用训练胜出的证据。扩展验证重算全部候选、成功动作、连续前缀与独立账户，但不重训，也不认证历史临时故障的发生。

本轮再次出现 Windows 个别进度事件追加遇锁后转 stderr 的已知提示；不声称 events.jsonl 收集到全部进度。结构化 trials、候选和账户归档由扩展验证核验。数值入口 `artifacts/e2e-m0-windows-outer-evaluation-20261023-001/acceptance.json` 为 PASS，手算权益 10290 元、会计残差 0，求解参考及三个光滑差分步长通过；仅为本机 CPU 数值参考。

最终完整主项目命令：

```powershell
artifacts/environments/e2e-clean/Scripts/python.exe -m pytest -q tests --tb=short --junitxml=artifacts/outer-evaluation-full-regression-20261023-001.xml
```

584 passed，0 失败/错误/跳过，控制台 1203.12 秒（JUnit 1202.855 秒），1669 条既有 Starlette/httpx 与上游 CVXPYlayers/NumPy 弃用提示。进程正常返回 0，结束后核心身份仍为 `86eb170d...88f94`；不与 105 项定向、历史 525 项或独立资源 17 项累加。该批次包含原预测/分数/目标/网站/worker、原训练/候选审查及新增外层失败路径。

本轮源码交付 `artifacts/releases/20261023-outer-evaluation-source-001/`，库存、工作区/stage/ZIP 逐文件内容、CRC 和文档链接的检查结果记录在 `artifacts/outer-evaluation-source-verification-20261023-001.json`。源码包不含本机环境、行情、权重或训练产物；新包不继承历史导出安装/三轮重训结论。当前核心本机合成软件范围的 R03-F 已验收，R03-S、代理资格、R04-D、N04/N05、市场优势及实际部署仍未关闭；没有推送 GitHub 或部署服务器。

## 2026-10-23 R03-S 当前源码包普通安装与自包含重训

当前核心身份为 `sha256:86eb170d21c70654a2b2e5985209dc1c4f9ce0fa6def695e55f16cd46c088f94`。报告：`artifacts/outer-evaluation-installation-20261023-001.json`，helper 和命令日志：`artifacts/outer-installation-logs-20261023-001/`。

冻结源码包 `artifacts/releases/20261023-outer-evaluation-source-001/quant-workbench-source.zip` 共 284 个文件，ZIP CRC、stage、workspace 清单均一致，SHA256 为 `10e07465ef2b942ce6a1d686b84fcadd5d3a5dfdc7754815265b4a7a00624351`。包被解压到一次性构建目录后，以普通非 editable 安装进入新环境；`qwb` 和 `ashare_tcost` 均从该环境的 `Lib/site-packages` 导入，`uv pip check` 通过，导入后的实现身份仍为当前核心 hash。

自包含脚本重新生成 110 日/24 资产/seed 17 合成数据，先执行一轮真实预测 GRU 预训练，再执行两轮效用 GRU 和完整候选审查。与工作区参考运行比较了源预训练参数、标签缩放、效用所选及全部候选的参数、优化器/CPU RNG、排除耗时与 RSS 后的确定性记录、辅助预测、候选连续/精确指标和独立账户结果，全部按声明范围一致；外层状态、失败计数、覆盖率、null 结果与比较资格也一致。报告状态为 PASS，`comparison_eligible=false`、`economic_efficacy=NOT_ESTABLISHED`。

该证据只支持同一台 Windows 机器的缓存依赖和 CPU、单折单 seed 软件重现。它不证明冷机器安装、Linux/GPU、非实现者复现、公开数据授权、市场效用或服务器部署；旧环境、工作区源码和历史文件没有被修改。

文档同步后生成新的 source-only 包 `artifacts/releases/20261023-outer-evaluation-source-002/quant-workbench-source.zip`，内容核验报告为 `artifacts/outer-evaluation-source-verification-20261023-002.json`，284 文件、ZIP CRC 和 230 个本地文档链接均通过，ZIP SHA256 为 `b57abf89d2baf6386cfb2e05db246207de5cb23e01618853dd0c802b91d191fe`。该包只同步文档，不改变 E23 的代码身份或重训结果。
