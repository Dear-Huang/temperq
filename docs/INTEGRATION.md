# 主目录整合记录

日期：2026-10-22。唯一开发目录：`D:/huang/quant-workbench`。

## 来源与去向

| 原目录 | 新位置 | 处理依据 |
| --- | --- | --- |
| `D:/huang/quant-workbench` | 保持原位 | 当前主仓库，保留 `.git` 与已有未提交 Bench 改动 |
| `D:/huang/ashare-tcost` | `third_party/ashare-tcost/` | 完整模块纳入主仓库；原 Git 元数据保留到 `archives/git-history/ashare-tcost.git` |
| `D:/huang/qwb_release_1022e` | `archives/releases/qwb_release_1022e/` | 精简发布副本；公共源码与主目录一致，不覆盖主目录 |
| `D:/huang/qwb_release_test` | `archives/releases/qwb_release_test/` | 旧代码快照，与当前公共文件有 17 处内容差异，保留追溯 |
| `D:/huang/qwb_clean_1022` | `archives/releases/qwb_clean_1022/` | 旧干净安装验收目录，包含本地环境与日志 |
| `D:/huang/qwb_release_1022` | `archives/releases/qwb_release_1022/` | 旧发布产物 |
| `D:/huang/qwb_release_1022b` | `archives/releases/qwb_release_1022b/` | 旧发布产物 |
| `D:/huang/qwb_release_1022c` | `archives/releases/qwb_release_1022c/` | 旧发布产物 |
| `D:/huang/qwb_release_1022d` | `archives/releases/qwb_release_1022d/` | 旧发布产物 |
| `D:/huang/qwb_release_final` | `archives/releases/qwb_release_final/` | 旧发布产物 |

迁移保留原有压缩包、说明、旧环境与日志。归档仅用于追溯，内部绝对路径和虚拟环境不保证迁移后可执行。不要在归档中继续开发或运行安装器。

本次未改动 `D:/huang` 中其他研究项目，也未迁移整个公共 `data` 目录。`config.local.yaml` 仍使用原机器的 `E:/data` 行情/缓存和 token 文件；这些资料不属于可发布源码。`third_party/Kronos` 和 `models/` 保留在主目录，但按原设计不上传。

## 代码衔接

- Windows/Linux 安装器固定使用 `third_party/ashare-tcost`，不再查找父目录的成本库；已有主目录 `.venv` 的 editable 安装也须重指向新路径。
- 根 `.gitignore` 只放行成本库源码，继续忽略其 `runs/`、本地数据、归档、压缩包、前端构建缓存和模型权重。
- 发布脚本默认输出 `artifacts/releases/`，统一解压顶层为 `quant-workbench/`。完整源码包包含网站、部署、CI；本地用户包只包含训练工作台和成本库。
- `website/` 和 `share_site/` 保持同源架构；网站数据库可用 `QWB_SITE_DB` 指定，Docker 使用持久化卷。
- Bench 的既有冻结协议和历史分数没有为目录整理重写。它们仍是原机器的研究证据，迁移到另一机器需要重新建立协议；不能把改过的路径当成原协议。

## 成本库来源

纳入版本：`a5dd20509ee78c80a557b831e1f2efd5060ddac8`，迁移前工作区干净。与最新本地发布包中共同存在的 11 个文件内容相同。

查看原历史（只读）：

```powershell
git --git-dir=archives/git-history/ashare-tcost.git log --oneline -5
```

不要将这个历史目录当成主仓库的子模块。新的成本库修改直接由主仓库管理，原许可与验证说明继续保留。

## 后续约定

只改主目录源码；所有新运行进入 `runs/`，所有新下载包进入 `artifacts/`。新机器使用源码安装器重建环境，不能复制 `.venv`。私有数据/密钥只填本机配置，公共模板仅写相对路径与占位符。上传和打包都先运行 `scripts/check_repository.py`。
