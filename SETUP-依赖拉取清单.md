# 依赖项目拉取清单

> 配套文档：`SPEC.md`（v0.2 草案）。
> 本清单只解决一件事：**把 SPEC 里点名要复用的外部项目拉到本机，并说清楚每个项目各自的坑。**
> 编制日期：2026-10-20。文中的仓库地址、默认分支、许可证均已逐项核验。
> 版本号一律留占位符，**不预先填死** —— 按 SPEC §16「M2 固定验收负载：数值在 M1 结束时冻结」，所有版本在 M1 结项时统一冻结并写入本文件底部的锁定表。

---

## 0. 先读这三条总原则

**1）能装包就别克隆。** 本项目里"库"和"源码"是两回事，混着来是后面环境崩掉的头号原因：

| 类型 | 处理方式 | 适用项目 |
|---|---|---|
| **运行时依赖**（当库用） | `uv pip install`，**不 clone** | LightGBM、MLflow、huggingface_hub、LlamaIndex、Docling、chronos-forecasting、tushare |
| **底座模型**（代码 + 权重） | 代码按需 clone 或 pip；**权重单独从 Hugging Face 按 revision 下载** | Kronos、Chronos-2 |
| **参考实现**（读代码、抄思路） | clone 到 `third_party/`，**不进 requirements** | Qlib |
| **外部工具**（不是 Python 包） | 各自装 | uv、Claude Code / Codex |

**2）一切锁定到具体 commit/tag，绝不跟 main。**
每个项目都要记录 `仓库 + commit 或 tag + 许可证`，写进模型包的「环境锁定文件」（§11.1）。这是 §12 里 R3（报告重现）、R4（重训重现）复现分级的前提，没有它 M3 那句"另一台机器达到 R1、R2"就是空话。

**3）克隆一律浅克隆 + 固定 ref。** 课题组共用时，全量历史没有意义，只会拖慢 CI。

---

## 1. 第 0 步：先装 uv

SPEC §2 已明确：用 uv 建虚拟环境，自动检测显卡和 CUDA，选对应 PyTorch wheel，无显卡退回 CPU。所有后续步骤都建立在它上面。

```bash
# Linux / macOS（Windows 的 Git Bash 里也能用这条的 wget 版本）
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows（PowerShell，不需要管理员）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 兜底：任何环境都能走 pip
pip install uv
```

```bash
# 建环境（Python 版本建议 3.11；Kronos / Docling 都要求 3.10+）
uv venv .venv --python 3.11
uv pip install -r requirements.txt   # 团队内请改用 .lock 文件
```

> ⚠️ **uv 默认装 CPU 版 torch。** 要 GPU 必须显式指定 CUDA 源，否则你会发现 5060 没被用上：
> ```bash
> # 例：CUDA 12.x 的 wheel 源，具体版本号在 M1 实测后定
> uv pip install torch --index-url https://download.pytorch.org/whl/cu128
> ```
> 仓库：`https://github.com/astral-sh/uv`（Apache-2.0，默认分支 main）

---

## 2. SPEC §2 点名复用的 8 个项目

| # | 项目 | 在 SPEC 里的角色 | 上手阶段 | 获取方式 | 许可证 | 仓库 |
|---|---|---|---|---|---|---|
| 1 | Qlib | 参考实现，**不作为运行时依赖** | M0 | clone 参考 | MIT | `microsoft/qlib` |
| 2 | LightGBM | MVP 主模型 | M0–M2 | pip | MIT | `lightgbm-org/LightGBM` ⚠️ |
| 3 | Kronos | 第二个模型底座 | M2 | clone + HF 权重 | MIT | `shiyu-coder/Kronos` |
| 4 | Chronos-2 | 底座备选（协变量档） | M2 后评估 | pip + HF 权重 | Apache-2.0 | `amazon-science/chronos-forecasting` |
| 5 | Docling | 研报/公告解析 | M5 | pip | MIT | `docling-project/docling` |
| 6 | LlamaIndex | RAG 检索 | M5 | pip | MIT | `run-llama/llama_index` |
| 7 | MLflow | **只做实验记录** | M1 | pip | Apache-2.0 | `mlflow/mlflow` |
| 8 | huggingface_hub | 权重下载 + 发布 | M2–M3 | pip | Apache-2.0 | `huggingface/huggingface_hub` |

---

### 2.1 Qlib —— 参考，别当依赖

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/microsoft/qlib.git`（默认分支 `main`） |
| 许可证 | MIT |
| 文档 | https://qlib.readthedocs.io/ |

```bash
# 只看文档就够的话，可以不克隆
git clone --depth 1 https://github.com/microsoft/qlib.git third_party/qlib
```

**注意事项**
- **只参考，不引入运行时依赖。** Qlib 自带 Alpha158 / Alpha360 因子集和它的数据集规范，与本项目 §4 的时点数据契约（record_id / revision / available_time / as-of 选择）**不是一套东西**。直接拿它当数据层，等于绕过 §4，M0 的校验器就白写了。
- 真要借鉴的是：任务与实验的组织方式、benchmark 部分。**抄思路，不抄接口。**
- 真要用它的 RD-Agent 等组件，单独开议题评估，不要顺手拉进主环境。

---

### 2.2 LightGBM ⚠️ 仓库地址已变更

| 项 | 值 |
|---|---|
| 新仓库 | `https://github.com/lightgbm-org/LightGBM.git`（默认分支 `main`） |
| 旧仓库 | `microsoft/LightGBM` —— **已迁移，访问会 301 跳转** |
| 许可证 | MIT |

```bash
# 日常使用只用装包，不需要克隆源码
uv pip install "lightgbm==<M1 冻结版本>"
```

**注意事项**
- ⚠️ **旧地址 `microsoft/LightGBM` 目前还能用（301 自动跳转），但 GitHub 已把仓库迁到 `lightgbm-org` 组织下。任何脚本、文档、CI 里写死的旧地址，请现在就改掉**，否则将来跳转失效时会莫名其妙挂掉。
- **模型请保存成文本格式（`.txt`）。** 这直接对应 §11.2 的执行边界：safetensors、LightGBM 文本模型属于"默认自动加载"的安全格式；**pickle 类权重不自动加载**。入库前先确认产出的是文本格式。
- 可复现相关参数要在 M2「固定种子数（3）」之前定死并写进配置：`deterministic`、`force_row_wise`、`num_threads`（线程数会影响结果）、随机种子。否则 §9 的"种子噪声地板"测出来的是环境噪声，不是模型差异。

---

### 2.3 Kronos —— MVP 之后要加的那个底座

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/shiyu-coder/Kronos.git`（默认分支 **`master`**，注意不是 main） |
| 许可证 | MIT |
| 权重（HF） | `NeoQuasar/Kronos-mini`(4.1M) / `Kronos-small`(24.7M) / `Kronos-base`(102.3M) / ~~Kronos-large(499.2M)~~ **未开源** |
| 权重（Tokenizer） | `NeoQuasar/Kronos-Tokenizer-base` / `Kronos-Tokenizer-2k` |
| 论文 | arXiv:2508.02739 |

```bash
# 该仓库目前没有正式 release，因此按 commit 锁定，不按 tag
git clone --depth 1 https://github.com/shiyu-coder/Kronos.git third_party/Kronos
cd third_party/Kronos
uv pip install -r requirements.txt

# 权重单独下载，务必带 revision（commit hash），不要拉 latest
# HF_TOKEN 走环境变量，不要写进任何文件
hf download NeoQuasar/Kronos-small --revision <commit> --local-dir models/kronos-small
hf download NeoQuasar/Kronos-Tokenizer-base --revision <commit> --local-dir models/kronos-tokenizer-base
```

**注意事项**
- **上下文本长是硬约束**：Kronos-small / base 的 `max_context = 512`，只有 mini 是 2048。超过会被自动截断。这直接卡住 §6 里"特征回看窗口"的设计上限 —— 日频长回看窗口要么换 mini，要么降低输入频率。**这是选型号前必须算清楚的一笔账。**
- **污染风险必须先查**：论文称训练数据来自 45+ 交易所。对应 §6 末条："底座模型的预训练区间如果覆盖测试区间，报告首页标注污染风险。" 接入前必须搞清楚预训练数据的时间范围，并据此划测试区间；样本外优先选"知识截止日之后"的区间（§7.2 同款要求）。
- **large 未开源**，规划算力时不要按 499M 那个档位算。
- 仓库里有 `finetune/` 和 `finetune_csv/` 两个目录，M2 之后做 §9 的"冻结底座 vs 微调 vs 直接训练小模型"消融时直接可用。
- 默认分支是 `master`，写脚本时别手滑写成 `main`。

---

### 2.4 Chronos-2 —— 备选底座（先别急着上）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/amazon-science/chronos-forecasting.git`（默认分支 `main`） |
| 许可证 | Apache-2.0 |
| 模型 ID | `amazon/chronos-2`(120M) / `autogluon/chronos-2-small`(28M) / `autogluon/chronos-2-synth`(120M) |
| 技术报告 | arXiv:2510.15821 |

```bash
# 它不是独立仓库，是 chronos-forecasting 这个包里的一个模型族
uv pip install "chronos-forecasting>=2.0"

# 参考示例：仓库内 notebooks/chronos-2-quickstart.ipynb
```

```python
from chronos import Chronos2Pipeline
pipeline = Chronos2Pipeline.from_pretrained("amazon/chronos-2", device_map="cuda")
```

**注意事项**
- **它不在 MVP 路径上。** SPEC §3 的 MVP 是"A股规则包 + 日频模板 + LightGBM，然后加一个底座（Kronos）"。Chronos-2 的定位是**备选**，先做兼容矩阵（§17 待定问题：各底座实际支持的频率和显存需求），别同时维护两条底座路径 —— 这正是 SPEC §3 说"小 Transformer 暂缓，避免同时维护三条训练路径"的同一个道理。
- **它真正的优势在协变量**：原生支持 past-only / known-future 协变量（Chronos / Bolt 均不支持，只能外挂回归器）。这正好对应 §3「模型底座适配器声明支持的输入类型（单变量 / 多变量 / **协变量** / 文本特征）」里的协变量一档。
- 上限：max context 8192 / max prediction 1024，比 Kronos 宽得多。
- 官方主推 SageMaker / AutoGluon-Cloud 云端部署 —— **我们是本地部署，不要引入这些云依赖**，只用它的推理包即可。

---

### 2.5 Docling —— 研报/公告解析（M5）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/docling-project/docling.git`（默认分支 `main`） |
| 许可证 | MIT |
| 最低 Python | 3.10 |

```bash
uv pip install "docling==<M1 冻结版本>"

# CLI 用法：支持多格式、整目录
docling 研报.pdf --to md --to json
docling ./corpus --to md --output ./out

# 想要 HTTP 服务或给 agent 用，装这两个
uv pip install docling-serve        # FastAPI 服务
uv pip install docling-mcp          # MCP server
```

**注意事项**
- ⚠️ **首次运行会联网下载模型**（版面分析、表格结构、OCR 等），之后才能离线。离线机器必须**提前把模型缓存目录做进环境锁定文件**，否则 M3 在另一台机器上达不到 R1。
- ⚠️ **Docling 只产出内容，不产出 `available_time`。** 它解析出的正文/表格是"文档本身"，而 §4.1 的 text 格式必须有 `publish_time`。研报、公告的发布时间要从外部元数据（抓取时间、公告日期字段）补上，且必须是**发布时间**不是下载时间。补不上的，按 §4.6 末条处理：**在校验报告中标出，并降级为"非时点数据"**，不能默认当作时点数据塞进训练集。
- Docling 内嵌的模型各有自己的许可证（README 明确："代码 MIT，单个模型许可见原包"）。§11.1 要求包内声明"许可证声明：底座、代码、各数据来源 → 能否再分发、能否商用"，**这部分要单独核对，不能只抄 MIT。**
- 如果只想要 MCP 接入，可以 `uvx --from docling-mcp docling-mcp-server`，不进主环境。

---

### 2.6 LlamaIndex —— RAG（M5）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/run-llama/llama_index.git`（默认分支 `main`） |
| 许可证 | MIT |

```bash
# 只装 core 和真正用到的子集，别装 llama-index 全家桶
uv pip install "llama-index-core==<M1 冻结版本>"
```

**注意事项**
- **只装需要的子集。** 全家桶依赖极重，极易与 torch / cvxpy / osqp 抢依赖版本，直接触发 §2 那几个导入顺序坑。
- **版本必须 pin 死。** LlamaIndex 迭代快、API 变动大，小版本升级就可能改掉索引构建行为。升级必须跑 §7.2 的回归。
- §7.2 对 RAG 的四条硬要求，接入时逐条落实，缺一条报告就不能算数：
  1. 按 τ 时点检索（§4.4 as-of 规则，不是"检索最新文档"）；
  2. 固定 LLM 版本、提示词、**索引快照**（索引快照要能落盘 + 记录哈希）；
  3. 接受样本外验证，**优先选 LLM 知识截止日之后的区间**；
  4. 检索和回答分开评估，配一份"问题、答案、证据"测试集。
- 报告还要标注**多次运行的离散度** —— 同一套输入多跑几次，差异有多大，得实测出来写进去。

---

### 2.7 MLflow —— 只做实验记录

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/mlflow/mlflow.git`（默认分支 **`master`**） |
| 许可证 | Apache-2.0 |

```bash
# 推荐装 skinny 版：不含 Flask/alembic 等服务端重依赖
uv pip install "mlflow-skinny==<M1 冻结版本>"

# 用本地文件后端，不要起 server
export MLFLOW_TRACKING_URI=file:./mlruns
```

**注意事项**
- ⚠️ **SPEC §8 原文：MLflow 只负责实验记录，不负责任务调度。** JobSpec 的 submit / status / logs / artifacts / cancel / retry 是我们自己的第 8 节任务服务，**不要让 MLflow 充当调度后端**，也不要把它的 Model Registry 当成模型包。
- 必记字段（§6 折清单）：`fit_cutoff`、训练样本范围、最大 `label_end_time`、**数据快照 ID**、模型版本。少任何一个，R3 就断了。
- 装 `mlflow-skinny` 而不是 `mlflow`，能显著降低与 pandas / osqp 的依赖冲突面。
- tracking 用本地目录即可，分享站（M4）读取的是元数据，不依赖 MLflow 在线。

---

### 2.8 huggingface_hub —— 权重下载与发布（M2–M3）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/huggingface/huggingface_hub.git`（默认分支 `main`） |
| 许可证 | Apache-2.0 |

```bash
uv pip install "huggingface_hub==<M1 冻结版本>"

# 国内网络走镜像
export HF_ENDPOINT=https://hf-mirror.com
```

**注意事项**
- ⚠️ **下载权重必须带 `revision`（commit hash），不能拉 latest。** 这是 §12 R3「同数据快照 + 同环境」的前提之一，权重漂移了报告就重现不了。
- ⚠️ **Token 绝不能进仓库。** 用 `~/.cache/huggingface/token` 或环境变量 `HUGGINGFACE_HUB_TOKEN`。对应 §11.2"密钥和数据扫描作为补充检查"、§15"LLM 内容检查只查违规：…泄露的 key"。**实验模式也不能绕过密钥扫描**（§11.2 原文）。
- 发布时只推**白名单清单里的文件**（§11.2："发布目录按清单白名单生成"），不要整目录推。

---

## 3. 底座共用：SPEC §2 点名的环境坑

这几条是 SPEC 原文记录的"已知本机坑"，**在装完上面任何一套 GPU 依赖之后都要复验一遍**：

| 坑 | 表现 | 处理 |
|---|---|---|
| **torch 必须先于 cvxpy 导入** | 反过来会崩 | 在入口文件顶部最先 import torch |
| **pandas 先于 osqp 导入** | **段错误**（0xC0000005，rc=139），没有 Python 报错栈，极难定位 | 保证 **osqp 在 pandas/numpy 之前**导入（2026-09-18 本机实测：`import osqp; import pandas` 正常，反之崩） |
| **Windows 编码** | 中文路径/内容乱码、读写失败 | Windows 下统一以 `python -X utf8` 运行 |
| **CUDA wheel 不匹配** | 显卡不被使用，静默跑 CPU | 按 §1 显式指定 index-url；无显卡环境回退 CPU 并在报告注明 |

建议在仓库里放一个 `sanity_imports.py`，按固定顺序 import 一遍，作为 CI 的第一道关卡：

```python
# sanity_imports.py —— 顺序不要改！改了就会踩 §2 的坑
import torch          # 必须最先：先于 cvxpy，也先于 pyarrow/pandas（否则 c10.dll WinError 1114）
# import osqp         # M6 组合模块用到时再开：必须在 pandas/numpy 之前
import pandas as pd   # qwb 包入口已内置此顺序守卫，import qwb 即可
import numpy as np
import lightgbm as lgb
# import cvxpy        # 在 torch 之后
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
print("torch.cuda.get_device_name(0)" if torch.cuda.is_available() else "CPU only")
```

---

## 4. 数据源连接器（M1）

### 4.1 Tushare —— A 股数据源

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/waditu/tushare.git`（默认分支 **`master`**） |
| 许可证 | BSD-3-Clause（**代码的许可，不等于数据的授权**） |
| 官网 | https://tushare.pro |

```bash
uv pip install "tushare==<M1 冻结版本>"
# 国内网络
uv pip install tushare -i https://pypi.tuna.tsinghua.edu.cn/simple
```

```python
import tushare as ts
ts.set_token("<TOKEN>")   # 走环境变量，不要硬编码
pro = ts.pro_api()
df = pro.daily(ts_code="000001.SZ", start_date="20240101", end_date="20240630")
```

**注意事项**
- ⚠️ **单位换算是头号数据事故源。** §4.6 原文："数量字段在连接器中声明来源单位，导入时统一换算（例如 Tushare daily 的 **vol 单位为手、amount 为千元**）。" 导入配方里必须显式声明，并在 M0 校验器里加断言。
- **积分制**：Pro 接口按积分分档（日线约 120 分，分钟线 2000+）。课题组要提前确认积分额度能覆盖 M1 的取数量，否则会取到一半被限流。
- **ST 历史名单接口覆盖区间待实测**（§4.6 与 §17 都点名了）。优先用 Tushare 历史 ST 名单接口，`namechange` 作为交叉检查或降级方案，**无论用哪种，都要在校验报告里记录覆盖缺口**。
- **时间戳带时区**：§4.3 规定所有时刻字段必须带时区；CSV 里无时区的时间，按市场时区解释（cn_a = `Asia/Shanghai`）。Tushare 返回的 `trade_date` 是日期标签不是时刻，别当时间戳用。
- **数据不随包分享**：§4.6 原文"配方可以分享，数据按授权处理，默认不随包分享"。发模型包时只发配方 YAML，不发数据。
- 连接器必须区分「接口失败」与「该期确实没事件」：前者**报错，不能静默跳过**（§4.6 末条）。这条是取数正确性的生命线。

---

## 5. 研究助理外挂（M5，都是外部工具）

| 工具 | 安装 | 许可证 |
|---|---|---|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | 见 README（非标准开源许可） |
| Codex | `npm install -g @openai/codex` | Apache-2.0 |

> 版本以各自官方文档为准，别照抄本文的版本号。

**注意事项**
- §7.3 原文：**MCP server / skill 调用的是第 8 节任务服务的接口，不另写一套逻辑。** 外挂只是第 8 节 JobSpec 的一个客户端，和 UI / CLI 平级，不允许另起炉灶绕过任务服务。
- agent 生成的转换脚本属于用户本地代码，**输出必须通过校验器才能入库，默认不进模型包**。
- **API 费用由用户承担**（§7.3 原文）。做 LLM 标注或 RAG 前先估算 token 开销。

---

## 6. 一键拉取脚本

### 6.1 Bash（Linux / Git Bash）

```bash
#!/usr/bin/env bash
# third_party 拉取脚本 —— 用法：bash scripts/fetch_third_party.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/third_party"
mkdir -p "$OUT"

# 参考实现：clone 到指定 tag/commit，并把实际 commit 落盘留证
clone_ref() {  # clone_ref <目录名> <仓库URL> <tag|commit>
  local dir="$OUT/$1" url="$2" ref="$3"
  if [ -d "$dir/.git" ]; then echo "skip $1 (已存在)"; return; fi
  git clone --depth 1 --branch "$ref" "$url" "$dir" 2>/dev/null \
    || { git clone --depth 1 "$url" "$dir"; git -C "$dir" fetch --depth 1 origin "$ref"; git -C "$dir" checkout "$ref"; }
  git -C "$dir" rev-parse HEAD > "$OUT/$1.commit"   # 锁定留证，复现要用
  echo "ok   $1 @ $(cat "$OUT/$1.commit")"
}

# ---- 按里程碑放开，别一次全拉 ----
# M0：参考实现
clone_ref qlib    https://github.com/microsoft/qlib.git                "${QLIB_REF:-main}"
# M2：Kronos（仓库无 release，默认用 master，结项时务必换成具体 commit）
clone_ref Kronos  https://github.com/shiyu-coder/Kronos.git            "${KRONOS_REF:-master}"

echo "完成。请把 $OUT/*.commit 的内容抄进本文件底部的「版本锁定表」。"
```

> 用法说明：`clone_ref` 优先按 tag 浅克隆；仓库没有 tag 时（如 Kronos）自动降级为"克隆 → fetch 指定 commit → checkout"。无论走哪条路，都会把实际 HEAD 写入 `<名字>.commit`。

### 6.2 PowerShell（Windows）

```powershell
# scripts/fetch_third_party.ps1
$ErrorActionPreference = 'Stop'
$OUT = Join-Path $PSScriptRoot '..\third_party'
New-Item -ItemType Directory -Force -Path $OUT | Out-Null

function Clone-Ref {
    param($Name, $Url, $Ref)
    $dir = Join-Path $OUT $Name
    if (Test-Path (Join-Path $dir '.git')) { Write-Host "skip $Name"; return }
    git clone --depth 1 --branch $Ref $Url $dir
    git -C $dir rev-parse HEAD | Out-File (Join-Path $OUT "$Name.commit") -Encoding utf8
    Write-Host "ok   $Name"
}

# M0
Clone-Ref 'qlib'   'https://github.com/microsoft/qlib.git'     $env:QLIB_REF
# M2
Clone-Ref 'Kronos' 'https://github.com/shiyu-coder/Kronos.git' $env:KRONOS_REF
```

---

## 7. 许可证核对清单

§11.1 要求模型包内声明「许可证声明：底座、代码、各数据来源 → 能否再分发、能否商用」。这张表是它的输入，**发布前逐行核对**：

| 项目 | 许可证 | 代码可商用 | 需要额外核对的 |
|---|---|---|---|
| Qlib | MIT | ✅ | 仅参考，不分发 |
| LightGBM | MIT | ✅ | 仓库地址已迁至 `lightgbm-org` |
| Kronos | MIT | ✅ | HF 上权重的独立许可；**large 未开源** |
| Chronos-2 | Apache-2.0 | ✅ | 模型权重许可 |
| Docling | MIT | ✅ | ⚠️ **内嵌模型各有自身许可，必须单独核对** |
| LlamaIndex | MIT | ✅ | — |
| MLflow | Apache-2.0 | ✅ | — |
| huggingface_hub | Apache-2.0 | ✅ | — |
| Tushare | BSD-3-Clause | ✅ | ⚠️ **代码 ≠ 数据；数据授权另按 Tushare 条款** |
| uv | Apache-2.0 | ✅ | — |

**共同提醒**：MIT / Apache / BSD 管的是**代码**。模型权重、内嵌模型、以及各类数据源各有独立条款，这是三件事，不能混为一谈。

---

## 8. 拉完之后必做的 6 件事（验收）

1. **填版本锁定表**（见文件末尾）：仓库、ref/commit、许可证、拉取日期，逐项落实。
2. **跑通 §12 R1**：环境安装成功 → 模型加载 → 示例输入可推理。用**合成数据**跑，不要用真实数据（§12："一致性样例必须是合成数据，或者是授权允许分发的数据"）。
3. **实测显存**：把各底座在 RTX 5060 8GB 上的实际占用记下来，填进 §17 待定的"各底座实际支持的频率和显存需求（兼容矩阵）"。
4. **复验导入顺序坑**：跑一遍 §3 的 `sanity_imports.py`，Windows 上用 `python -X utf8 sanity_imports.py`。
5. **确认权重 revision**：所有 HF 权重的 commit hash 已记录且不被 latest 覆盖。
6. **确认 token 未入库**：`HF_TOKEN` / Tushare token 只存在于环境变量或 `~/.cache`，仓库里扫一遍确认干净。

---

## 9. 跟着 SPEC §17 走的待定项

拉取相关、需要实测后回填的：

- [ ] Kronos 预训练数据的时间区间 → 决定测试区间划分与污染风险标注（§6）
- [ ] Tushare 历史 ST 名单接口的实际覆盖区间与权限要求（§4.6 / §17）
- [ ] 各底座的实际显存需求与适配矩阵（§17）
- [ ] LightGBM / MLflow / Docling / LlamaIndex / huggingface_hub 的冻结版本（§16，M1 结束时定）
- [ ] 新闻源：授权与接口稳定性（§17）
- [ ] §10.2 提到的成本模型出自 **Wrong Ruler 稿**（属文献/稿件，非代码仓库）—— 接入前需复核参数，需拿到原文
- [ ] 各项目 clone 时的网络方案：国内访问 GitHub / HF 不稳，统一走 `HF_ENDPOINT=https://hf-mirror.com` 与 pip 国内镜像

---

## 10. 版本锁定表（M1 结项时填写）

| 项目 | 仓库 | Tag / Commit | 许可证 | 拉取日期 | 负责人 |
|---|---|---|---|---|---|
| Qlib | `microsoft/qlib` | _待填_ | MIT | | |
| LightGBM | `lightgbm-org/LightGBM` | _待填（pip 版本）_ | MIT | | |
| Kronos | `shiyu-coder/Kronos` | _待填_ | MIT | | |
| Kronos 权重 | `NeoQuasar/Kronos-small` | _待填_ | _待核_ | | |
| Chronos-2 | `amazon-science/chronos-forecasting` | _待填（pip 版本）_ | Apache-2.0 | | |
| Docling | `docling-project/docling` | _待填（pip 版本）_ | MIT | | |
| LlamaIndex | `run-llama/llama_index` | _待填（pip 版本）_ | MIT | | |
| MLflow | `mlflow/mlflow` | _待填（pip 版本）_ | Apache-2.0 | | |
| huggingface_hub | `huggingface/huggingface_hub` | _待填（pip 版本）_ | Apache-2.0 | | |
| Tushare | `waditu/tushare` | _待填（pip 版本）_ | BSD-3-Clause | | |
| uv | `astral-sh/uv` | _待填_ | Apache-2.0 | | |

---

_本清单随 SPEC 修订同步更新。仓库地址与许可证核验于 2026-10-20。_
