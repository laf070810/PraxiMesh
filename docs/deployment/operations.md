# 监控、排障、备份与恢复

[部署方案选择](README.md) · 用于已部署实例；首次安装分别见 [Compose](compose.md)或[发布协调器](production.md)。

## 常见问题先查这里

| 现象 | 检查与处理 |
| --- | --- |
| 页面正常，评测不可用 | 查看 `/health/` 的 `evaluation`、worker 日志与健康状态；首次自检、检查记录过期或心跳失效都会关闭评测。若执行过排空，还要检查暂停状态，使用原 token 恢复 |
| 提示额度或排队已满 | 默认每用户最多 2 个待处理任务，每 UTC 日 120 个 CPU 调用预约单位；这不是实际 CPU 秒。等待已有任务完成或额度恢复，不删除任务来绕过限制 |
| 模型不可用，评测正常 | Compose 本地模型检查 tutor 健康状态；已有服务检查 HTTPS、凭证和模型配置。模型故障不会改变正式成绩 |
| 容器重建后好像是新站点 | 先核对 Compose 项目名、原卷名和数据库路径；换目录/项目名可能创建另一组卷。不要删除旧卷或重新导入内容来修复 |
| 从其他电脑无法访问 | 核对监听地址、实际宿主端口、允许的 Host 及网络路由；Compose 默认 HTTP 8000，协调器按配置的域名/HTTPS 访问 |
| 协调器发布中断或维持维护页 | 保留原配置、状态和数据库，读取 `status` 后执行 `recover`；不要手改 journal、切回旧快照或新建同名实例 |

Compose 使用同一组文件查看 `ps`、`logs -f worker` 或 `logs -f tutor`，完整命令见 [Compose 操作](compose.md)。协调器的状态与恢复命令见[生产部署](production.md#中断失败与恢复)。

## 监控与故障处置

`GET /health/`：数据库可用时 HTTP 200；`evaluation` 单独表示判题是否可接收任务。内容服务正常而 judge 不可用是允许的降级。`gunicorn.conf.py` 的访问日志只记录 HTTP 方法、路径、状态和耗时；worker 记录任务 UUID 和结果类型，不记录源代码、密码、测试或数据库 URL。代理示例不启用访问日志，若运营需要 IP/请求头日志，须先明确脱敏和保留政策。

至少监测：HTTP 5xx/延迟、数据库连通性、worker 心跳年龄、排队数与最老任务等待、平台故障率、执行节点资源、遗留容器。阈值需依据试运行实测，当前没有可信的公网容量数字。

协调器部署的定时维护使用同一配置和状态目录，每分钟由可信部署账户执行：

```sh
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state maintain
```

它与发布共享文件锁和数据库会话锁，忙时可在下个周期重试；服务和 timer 示例见部署指南。不要让另一个后台 `manage.py maintain` 绕过协调锁写数据库。独立运行、未使用协调器的实例才直接调度该管理命令：Compose 使用 `docker compose exec -T web python -m core.selfhost manage maintain`（启用内置 AI 时带原两个 `-f` 参数），源码环境使用固定虚拟环境的 `manage.py maintain`。

维护将租约过期任务/等待超过 10 分钟任务记为平台故障，清理到期会话/限流桶。worker 活跃时也回收过期任务；**worker 全停时仍必须安排维护**，避免用户一直看到排队。

协调器部署发生中断时先查看 `deploy.py status` 与私有日志，再使用相同配置的 `recover`；状态记录不是实时健康证明。独立 gVisor 实例故障处置时，先关闭评测入口、停 worker，再按 `praximesh.judge=true` 标签逐个核对和清理自己的容器，修复后重新进行隔离/功能检查。不要使用全局 prune。数据库任务因 lease token 校验不会被迟到 worker 覆写。

## 备份与恢复

备份对象与部署方式对应：

| 方式 | 备份对象与入口 |
| --- | --- |
| Compose | 停止服务后备份完整 `app-data`、启用时的 `ai-credentials`、私有 `.env`；操作顺序和恢复权限见 [Compose 数据与更新](compose.md#数据停止与更新) |
| 源码开发 | 当前 `SQLITE_PATH` 指向的数据库及使用中的应用密钥；使用下方 SQLite 在线备份命令 |
| 发布协调器 | PostgreSQL 数据、私有配置、部署状态/journal、应用密钥及仍需恢复的发布包；下方 PG 备份与[协调器恢复](production.md#中断失败与恢复)配合使用 |

源码开发的 SQLite 不能只复制正在写入的文件。使用现有固定虚拟环境及在线备份 API：

```sh
mkdir -m 700 -p /private/backup-directory
.venv/bin/python manage.py backup_sqlite /private/backup-directory/praximesh-UNIQUE.sqlite3
```

目标文件必须不存在，写入权限 0600，备份执行 SQLite integrity_check。恢复演练应设置 `SQLITE_PATH` 指向备份的独立副本，运行 `manage.py check`、核对知识/题目版本/用户/提交数量，并通过测试账号验证登录、草稿、历史。演练先保持 `JUDGE_ENABLED=0`，不得覆盖现有数据。应用停止后才能将验证过的副本切换为活动库。

生产 PostgreSQL：使用只读备份角色和私有 `PGSERVICEFILE`/`.pgpass`（0600），避免在命令行暴露密码：

```sh
pg_dump --dbname='service=praximesh-backup' --format=custom --file=/private/backups/praximesh-UNIQUE.dump
pg_restore --list /private/backups/praximesh-UNIQUE.dump
# 由维护者创建独立、空的恢复验证数据库后：
pg_restore --dbname='service=praximesh-restore-test' --no-owner --no-acl /private/backups/praximesh-UNIQUE.dump
```

验证迁移状态、计数、题目摘要与私有记录的访问控制；记下恢复耗时和实际 RPO/RTO。不要将 `pg_restore --list` 当成已完成恢复验证。本地真实 PostgreSQL 已完成 pg_dump/pg_restore 和恢复后内容/私有记录核对；正式生产环境与其 RPO/RTO 尚未验证。

协调器在升级时冻结旧库，将完整备份恢复到候选代际库，逐表验证行、列定义和序列。candidate-6 已实际验证该流程及恢复后新增身份的序列行为。旧库、失败候选库和备份均不会被自动删除；它们是发布恢复和诊断资料，仍含个人历史数据。开放后的恢复保留当前数据库，不能借旧快照撤销新写入或恢复已删除账户。

备份加密并保存在不同故障域，限制解密角色，定期演练。维护者在公开发布前明确保留期限、用户删除后的备份处理和恢复时重放删除的机制；当前未定政策不能写成已承诺的隐私保证。
