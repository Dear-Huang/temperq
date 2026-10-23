# 服务器部署

本方案部署网站和模型社区，训练 UI/Bench 仍在本机或计算服务器通过 CLI 运行。镜像不会携带本地行情、模型、归档、虚拟环境或生产密钥。

## 准备

服务器需有 Docker Engine 和 Compose v2。域名 A/AAAA 指向服务器，开放 80/443，HF OAuth 回调地址设为 `https://你的域名/oauth/callback`。没有可用 IPv6 时不要设置 AAAA。

将 GitHub 仓库克隆或完整源码包解压到服务器，例如 `/opt/temperq`。不要使用不含网站的 local 用户包来部署网站。

```bash
cd /opt/temperq
cp deploy/.env.example deploy/.env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

编辑 `deploy/.env`，填入域名、证书通知邮箱、HF OAuth 客户端凭据和刚生成的随机会话密钥。`.env` 已被 Git 和 Docker 构建上下文忽略。公开部署不设置 `TEMPERQ_SITE_DEV`。

## 构建与启动

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail=100 site caddy
curl -f https://你的域名/healthz
```

Node 构建前端后拷入 Python 镜像；后端以普通用户 UID 10001 运行。Caddy 自动申请/续期 HTTPS，外部只开放 Caddy，后端不映射宿主端口。后端只运行一个 worker，以匹配 SQLite 连接和内存限流的当前实现。

如果 80/443 已被现有反向代理占用，可仅启动 `site`，按现有代理规范配置 HTTPS 和后端连接。不要同时启动两套占用相同端口的入口。

## 持久化与备份

数据库位于命名卷 `temperq_site_data` 内的 `/var/lib/temperq/site.db`；重建镜像不会清除它。Caddy 的证书与配置也使用独立卷。不要执行 `docker compose down -v`，它会删除命名卷。

在线一致性备份：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T site \
  python scripts/backup_site_db.py /var/lib/temperq/site.db /var/lib/temperq/backups 14
mkdir -p backups
docker compose --env-file deploy/.env -f deploy/compose.yaml cp site:/var/lib/temperq/backups/. ./backups/
```

将命令放入每日定时任务，并把 `backups/` 复制到服务器外保存。根目录 `backups/` 不应提交 Git。保留密钥文件的安全副本；会话密钥变化会使现有登录失效。

恢复前先备份当前数据库、停止 `site` 写入，再把选定备份恢复到卷中的 `site.db`，保持 UID 10001 可读写，启动后检查 `/healthz`、论坛和登录。上线前应在测试卷演练恢复，不要仅凭备份文件存在就认定可恢复。

## 更新

```bash
git pull --ff-only
docker compose --env-file deploy/.env -f deploy/compose.yaml build site
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

更新前备份；升级后检查健康、静态资源、OAuth 登录、发帖和重启后的数据持久化。当前数据库迁移在应用启动时进行，重大 schema 更改需另写迁移与回滚方案。

本机无 Docker，容器构建、DNS、证书签发、HF 回调与生产运行须在目标服务器验收。当前只交付源码、部署配置与本地软件验证，不声称这些服务器检查已通过。上线缺项和操作清单见 [发布复盘](../docs/RELEASE_READINESS_20261023.md)。

新部署使用 `TEMPERQ_*` 环境变量、Compose 项目 `temperq` 和 `/var/lib/temperq`。后端仍兼容旧 `QWB_SITE_*` 变量。若迁移以前的实例，先备份旧数据库并显式恢复到新卷；更改项目名不会自动迁移旧卷，不要因空站而删除旧卷。
