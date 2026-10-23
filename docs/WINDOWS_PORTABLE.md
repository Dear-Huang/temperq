# Windows 便携版：本轮交付与发布

本轮只做“免费数据 → 首份报告”和“Windows 自带运行环境”，完成即停。STRUCT、Qlib 训练实验 B、规则插件、GRU 不纳入本轮。公网服务器与 PyPI 不部署、不发布。

## 用户最短路径

下载 `TemperQ-windows-x64.zip` → 完整解压 → 双击 `start.bat` → 点击“一键下载免费数据（BaoStock）” → 等待完成 → “用这份数据继续” → 开始训练 → 查看报告 → 进入评价。建议 16GB 内存，无需显卡或开发工具。启动和合成训练可离线，免费行情下载需要联网。

候选包约 109MB，区别于旧约 50MB 的联网安装包。包含独立 CPython 3.11.15、锁定的 CPU 依赖、Microsoft C++/OpenMP 运行库、成本库、源码、示例、手册和组件许可证。不包含私人行情、token、模型或账本。解压后数据与结果写入本包 `data/`、`runs/`，升级前备份。

## 已验证和待验证

- BaoStock 0.9.4 实际匿名连接成功；40 股、2021-01-04 至 2026-10-22，共 56,160 条日线下载完成。旧 SDK 服务地址失效及新版 pandas 分页兼容问题已处理。
- 网页提交 LightGBM 训练并生成报告，下载完成页能回到已选中数据的训练页。1440/390 宽检查无横向溢出。
- 在迁移后的便携环境，通过 HTTP 页面入口完成真实下载 → 已选中该数据 → LightGBM 首份报告。第一次请求出现 BaoStock `10002007` 网络接收错误且未发布数据；页面重试成功，下载约 142 秒，80 轮/单种子训练约 5 秒，两个独立下载得到同一快照哈希。时间只代表本机该次操作，不含解压或首次失败等待，不是服务承诺。
- 网络错误、缺日、取消不发布半份快照；已发布历史不改写，再次运行只追加新日期。未发布区间失败后重新下载，不宣称按股票断点续传。
- 首次中文目录迁移测试发现 LightGBM 原生文件路径不兼容，已改用 Python UTF-8 读写模型字符串。修复后已在中文加空格的新目录，通过 `start.bat` 完成离线启动、字体加载、独立 worker 训练、保存模型与报告；PATH 仅系统目录，用户包禁用，Python 外网连接受审计钩子阻断。
- 随后 DLL 导入表核对发现 LightGBM 还依赖 Microsoft C++/OpenMP。构建器增加固定版本 `msvc-runtime==14.44.35112` 的原版 DLL（校验 Microsoft 数字签名、保留再分发条款）；验收脚本检查实际加载的 `msvcp140.dll` 和 `vcomp140.dll` 来自便携运行目录，避免开发机已有运行库掩盖依赖。
- 代码分两批 112 项定向回归通过，随后新增的“首条行情前节假日日历保留”回归也通过；最终免费数据模块 6 项通过。不把重叠执行累加成新增测试数。
- 新增手动 Windows CI 构建和迁移测试。尚未推送触发，不声称 CI 已通过。
- 尚无另一台电脑、普通用户独立完成流程的证据。不能把开发机验收叫作“所有小白均可一键成功”。

40 股名单是今天选出的入门样本，存在选择偏差；不是历史指数成分，也不是全市场。下载的是原始价格、后复权因子、历史 ST 和日历，涨跌停价按规则估算。训练报告只用于软件验收，不代表投资收益。免费接口可能中断或修订，用户须遵守数据源使用条款；不再分发其行情。

## 构建和验收

在 Windows x64 的源码仓库中，用独立的 python-build-standalone CPython 3.11 安装目录作为运行环境来源（不能用 venv）：

```powershell
uv python install 3.11.15
$runtimePython = uv python find --system --no-project --managed-python 3.11.15
python scripts/build_windows_portable.py artifacts/windows-portable --python-home (Split-Path $runtimePython)
python -X utf8 scripts/check_windows_portable.py artifacts/windows-portable/TemperQ-windows-x64.zip "artifacts/便携验收 test"
```

输出目录须尚不存在。构建时需联网安装锁定依赖；成品无需再安装。每个文件的 SHA-256、源码 commit、工作区状态、Python 和组件版本记录在 `MANIFEST.json`；ZIP 校验值在同目录 `SHA256SUMS.txt`。源码和组件许可随包保留。

命令行用户可在便携目录运行 `./temperq.bat run examples/run_smoke.yaml`。批处理使用相对路径；删除了 wheel 生成的、内嵌构建机绝对路径的 EXE 启动器，避免迁移后误用旧解释器。

验收脚本解压到中文/空格目录、核对文件哈希，将 PATH 限为系统目录、禁用用户 Python 包和开发源码路径，并通过 Python 审计钩子阻断父进程及 worker 的外网连接。随后实际执行 `start.bat`、访问本机网页/字体、提交 CPU 训练、等待独立 worker 完成并检查模型与报告。结果写入解压目录上层 `acceptance.json`。此测试不等于另一台物理电脑验收。

GitHub：推送后手动运行 **Windows portable package** workflow，下载其 artifact。它只构建和验收，不自动发 Release。检查日志、ZIP、SHA256SUMS、acceptance 后，再在 GitHub Releases 上传 ZIP 与校验文件并附上限制说明；不要上传整个 artifacts 或含用户数据的解压目录。

## 下一步只交付一次用户试用

找 2–3 位无开发经验的 Windows 用户，给同一 ZIP，不预装环境、不口头代操作。记录：解压/启动耗时、数据是否取得、网络/权限报错、是否需要人工帮助、首份报告耗时，以及能否理解“证据不足”。建议连续两周观察纸面账户，但不能用两周记录证明盈利。试用表见 [USER_TRIAL.md](USER_TRIAL.md)。

## 本机前向账户保护

中文路径修复改变了 `daily.py`，现有冻结账户按设计拒绝继续。旧主账户 `lgbm_forward_20261023` 在修复时为 **0 条记录**，未改写其配置、模型或账本，保留供审计；新增 `lgbm_forward_20261023_iofix`，相同模型和协议、相同全市场数据源，重新冻结实现并保持 2026-10-23 起始日。原 Windows 每日任务已备份并切换到新账户，仍为登录时 17:30 先更新全市场数据再记账。数据不到位则受阻；尚未发生的收盘记录不算完成。其他已有账户不自动改哈希，保留旧记录，使用新实现须另建账户。
