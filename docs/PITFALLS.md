# 已知坑清单

新连接器、新底座接入前过一遍；踩到新坑即补一行（现象 → 根因 → 处理 → 代码位置）。

## 数据源（Tushare 兼容服务 gyzcloud）

| 现象 | 根因 | 处理 |
|---|---|---|
| namechange 全量 offset 翻页，后几页与前页重复（25000 起一页重 392 条），总 34762 条仅 20505 唯一 | 服务端无稳定排序 | 按年窄化查询；`paged()` 检测页间重叠即报错（`connectors/tushare.py`） |
| 无数据年份返回空表且不带 fields | 服务端行为 | 空页结束翻页；非空页缺 key 列报 ApiError |
| 拉了约 100 天后报 `code=429 未开启 gzip，当前限制线程` | 不带 `Accept-Encoding: gzip` 的客户端被限流 | 请求带 gzip 头；429 退避重试，重试用尽才报错 |
| 多进程并行拉取不提速（5 进程≈1 进程，约 30 天/分钟） | 服务端按 token 限速 | 单进程即可；全 A 股 2018–2025 约 1 小时 |
| 并行拉取时缓存写入报 `WinError 5 拒绝访问 .tmp -> .parquet` | 多个进程写同一 key 时用了同名临时文件；Windows 下目标被占用时 rename 失败 | 临时文件名带进程号；rename 失败且目标已存在时用对方的文件（`connectors/cache.py`） |
| `daily.vol` 单位手、`amount` 单位千元 | 接口约定 | 连接器换算为股、元（`connectors/cn_a.py`） |
| 财务表 `end_date` 与 `ann_date` 可差两年半（茅台 1998 年报 2001 年才公告） | 报告期≠可用时间 | 财务按 `ann_date` 定 available_time（observation 格式） |
| `stock_st` 按 `ts_code` 查单页上限 1000 行 | 分页 | 用 `paged()` |
| 北交所涨跌停价按四舍五入算，一半对不上 | 北交所截断取整：涨停向下、跌停向上 | 规则包 `rounding: truncate` |
| `stk_limit` 返回 99999.99 / 0 | 当天不设涨跌幅（如上市首日） | 当作无涨跌停，不当作价格 |
| S 股、退市整理期的涨跌幅不是 10% | 特殊档位，规则包未收录 | 核对报告单列原因；入规则包前先补档位 |
| 旧服务 teajoin 的 key 已停用 | 换服务商 | 用 gyzcloud；token 只从文件或环境变量读，不进日志 |
| tushare SDK 的 `set_token` 会把 token 写进用户目录文件 | SDK 行为 | 直接走 HTTP，不用 SDK |

## 时点与口径

| 现象 | 根因 | 处理 |
|---|---|---|
| 停牌被记成 0 价 0 量，`isna` 查不出 | 数据源编码 | 校验器 `HALT_ENCODED_AS_ZERO`；停牌行价格留空 |
| 用今天的 ST 标记筛历史股票池 | 前视 | 配方要求 `point_in_time: true`；ST 用逐日 `stock_st` |
| namechange 推出的 ST 包含已退市股票 | 改名记录不随退市截断 | 交叉核对前按上市/退市日期截断 |
| shift / pct_change 在全表上算，跨股票泄漏 | 未按 code 分组 | 特征一律 groupby(code) |

## 环境（本机）

| 现象 | 根因 | 处理 |
|---|---|---|
| github.com 超时、huggingface.co 连接被重置；hf-mirror.com 可通 | 网络 | 本机代理 `127.0.0.1:10808`（http 与 socks5h 均可）；git 用 `-c http.proxy=`，Python 用 `HTTPS_PROXY` |
| `python -I` 下找不到刚 `pip install --user` 的包 | `-I` 隔离模式不加载用户 site-packages | 处理不可信下载时才用 `-I`；运行自己的脚本不用 |
| Kronos 权重目录带一个 `model.safetensors.baiduyun.uploading.cfg` 空文件 | 上游仓库残留 | 无害，打包时不列入白名单即可 |
| pandas 先于 osqp 导入即段错误（rc=139 / 0xC0000005） | 二进制运行时初始化顺序 | osqp 先于 pandas/numpy 导入 |
| pyarrow（pandas 会带起它）先于 torch 导入，torch 报 `WinError 1114 c10.dll` 初始化失败；单独 `import torch` 正常 | 同类（2026-10-20 实测：numpy、yaml 在前无碍，pandas、pyarrow 在前必崩） | 安全顺序 torch → osqp → pandas；已写进 `qwb/__init__.py` 守卫 |
| torch 晚于 cvxpy 导入会崩 | 同类 | torch 最先 |
| Windows 中文输出乱码 | 默认编码 | `python -X utf8` |
