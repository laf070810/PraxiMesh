# 部署、维护与恢复手册

修订：2026-10-30。初始部署目标 Linux amd64。其他架构必须换用对应镜像 digest 并重新验证；当前不声称支持全部架构。当前能力与各批验收状态见[需求追踪](requirements.md)，初始基线见 [acceptance.md](acceptance.md)。

## 本地应用

README 的 uv 命令运行非执行功能。SQLite 默认位于仓库忽略文件 `db.sqlite3`。在禁止写用户缓存的环境可设置 `UV_CACHE_DIR=/tmp/praximesh-uv`。依赖输入为 `pyproject.toml`，完整解析锁为 `uv.lock`，容器用带 hash 的 `requirements.lock`。

本地容器一键启动，无需宿主 Python 或手工生成密钥：

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f web
```

默认监听 `0.0.0.0:8000`，健康后在其他电脑打开 `http://服务器IP:8000`；同机也可使用 `http://127.0.0.1:8000`。可用 `PRAXIMESH_BIND`、`PRAXIMESH_PORT` 调整宿主监听地址和端口；容器内仍监听 8000。Compose 的 `APP_ALLOWED_HOSTS` 默认 `*`，接受直接 IP/主机名访问；显式限制时使用逗号分隔的主机名或 IP，并保留 `127.0.0.1` 供健康检查。它不修改 Python 开发环境或生产设置的默认主机规则，也不关闭 CSRF 校验。

Compose 使用固定的 Python 基础镜像，`PYTHON_IMAGE` 仍可由操作者显式覆盖。应用非 root、只读根、带 init、删除 capabilities，有内存/PID 限额；不挂宿主目录或 Docker socket。判题和 AI 关闭，不要求 GPU 或模型服务。该入口使用 HTTP 体验配置；正式公开服务继续按下述生产流程部署。

`core.preview` 在 `app-data` 命名卷中生成并保留 0600 的随机密钥 `/data/preview-secret.key`。旧配置若提供 `APP_SECRET_KEY`，继续使用该值，重启时保持相同配置。密钥丢失或更换会使原会话失效，不能将重新生成密钥当作恢复操作。密钥与 SQLite 一起备份，备份按私有数据保护。

仅当 `/data/praximesh.sqlite3` 不存在时，才在同一卷的临时目录运行迁移，并导入基础题库、英文译文、项目及挑战；全部成功后发布新数据库。失败会退出，修复后再次启动即可重试。已有数据库只迁移，不重导或撤回已有内容。旧版 Compose 卷若缺少译文、项目或挑战，应由维护者按内容规范审查后显式导入，不能通过删除原数据库“补齐”。

`docker compose down` 保留卷；`docker compose up -d` 使用已有镜像重新启动；源码更新后用 `up --build -d`。`down -v` 会删除数据，请勿用于日常停止或恢复。这里的自动迁移面向本地体验；生产使用下述协调器的备份、排空与代际恢复流程。

本地重放该入口的检查：`python3 scripts/compose_acceptance.py`。只要求宿主 Docker/Compose 与 Python 标准库；脚本使用唯一项目名、临时端口和合成数据，通过宿主非回环 IP 验证页面与同源草稿 POST，确认跨站 POST 仍被拒绝，验证初始化、账号/草稿/会话/自定义内容与密钥保留，并清理自有容器、网络和卷。设计见 [RFC-015](rfcs/015-compose-preview.md)。

更新依赖：`uv lock --upgrade-package PACKAGE` → `uv sync --frozen` → 所有相关测试与审计 → `uv export --frozen --no-dev --no-emit-project --format requirements-txt --no-header --quiet -o requirements.lock`。基础镜像也要跟随安全更新，不因 digest 固定而停止升级。记录更新前后镜像摘要及验收。

## 生产应用的前提

使用 PostgreSQL（与本地 SQLite 区分），数据库连通性与并发需真实测试。数据库 owner 仅供受保护的迁移、备份恢复与部署检查，日常角色只取得必需的表/序列权限。采用 `deploy/app.env.example` 的生产设置，以独立生成的至少 50 字符密钥替换占位值；协调器配置方式见[部署协调指南](deployment-coordination.md)。

- `PRAXIMESH_MODE=production` 会要求随机密钥、显式主机和 PostgreSQL。
- `DEBUG` 关闭、HTTPS 重定向、HSTS、Secure/HttpOnly 会话和 CSRF cookie；静态资源从容器内读取。
- 手工配置 TLS 代理时，使用 `deploy/Caddyfile.example` 与其导入的 `deploy/proxy.caddy`，将两者放在同一目录；协调器生成并管理专用 Caddy 配置。只有代理可达后端；代理覆盖 X-Forwarded-Proto 后才能设 `TRUST_TLS_PROXY=1`，并将实际连接后端的代理 IP 加入 `TRUSTED_PROXY_IPS`。缺少可信地址时应用拒绝启动；协议值必须恰为 `http` 或 `https`，逗号列表不能授权 HTTPS。Gunicorn 的 `forwarded_allow_ips` 保持空，由应用执行这项校验。
- 代理无条件重写 X-Real-IP，避免伪造或所有用户共享代理 IP 的额度。不受信来源的转发头始终忽略，额外的 CDN/多级代理需单独设计并验证信任链；IP 白名单不能替代后端网络访问控制。
- 公共注册默认关。维护者确认运营策略和额度后才设 `REGISTRATION_OPEN=1`。
- `JUDGE_ENABLED=0` 时关闭执行但继续提供内容、账号、草稿。
- Web 镜像不安装 Docker 客户端，不挂载 Docker socket，不需要云基础设施凭证。

发布协调器先校验不可变制品，暂停并排空任务，再切换维护页、停止旧进程并冻结旧库；将备份恢复到候选库，比较真实数据、列定义和序列后才迁移。候选 worker 重跑隔离探针，核对指定实例的心跳及真实正确/错误提交，通过后激活代理。初次内容导入需要显式选项，升级不会覆盖运营内容。完整操作和资源限制见[部署协调指南](deployment-coordination.md)，决策见 [ADR-008](adr/008-deployment-generations.md)。

候选开放前的失败可恢复旧数据库和旧进程。任何候选代理配置可能持久保存前，journal 先保存开放意图；此后 `recover` 保留当前库前向恢复，不能自动切回旧快照。这一边界保护已经发生的写入与删除。不要修改 journal、手动换 token 或绕过它启动另一代。

当前 CI 定义覆盖应用、内容、浏览器、WASI/HTTPS 和制品验收，另有受保护的 gVisor workflow。部署入口 [deploy.yml](../.github/workflows/deploy.yml) 已实现，只接受手工触发，默认禁用；维护者配置默认分支保护、`production` 环境、独立部署 runner 与可信制品传输后才可启用。托管 CI 执行和实际部署目标尚未验证，公有证书/续期、系统服务及持续容量仍需目标主机验收。

真实 Caddy、生产 Gunicorn、PostgreSQL、WASI 的临时回环 HTTPS 验收已通过，可按[部署验收指南](production-acceptance.md)重放。candidate-6 另用同一不可变发布包完成四代部署，实际验证备份恢复、开放前中断、开放后写入/删除保留和代理重启。该次未改变 schema，也未验证失败迁移；详见[验收记录](acceptance.md)。

同源的 Web/worker 发布包已有实际导入和容器浏览器验证，构建、校验和导入命令见[制品指南](releases.md)。`release.py load` 导入固定镜像，后续激活使用 `deploy.py apply`。`requirements-judge.lock` 从 `uv.lock` 的 judge group 导出并与应用锁同时检查，worker 入口会在当前实例重跑真实安全探针。

持久暂停、按策略领取和 `judge_admission drain` 在发布前原子关闭新评测并保留已接收任务；超时后保持关闭，恢复需要原 token 和匹配的健康 worker。暂停本身只关闭评测入口，全站写入停止、代理切换与数据库恢复由协调器随后完成。首次引入暂停协议的 schema 时，仍须先停止不理解该协议的旧入口并排空旧 worker，详见[首次升级说明](deployment-coordination.md#首次升级)。

容器构建与烟雾检查也已加入 CI；可本地复现 `docker build -t praximesh:phase-a-check .` 和 `uv run python scripts/container_smoke.py`。脚本验证非 root、只读根、无外部网络情况下的迁移、内容导入及 HTTP/静态资源，结束删除临时容器；不运行用户提交代码。

Web 的数据库权限限制为知识/题目/健康记录只读，以及账号、会话、草稿、路径、任务、限流、队列锁、私有学习记录和社区内容的必要操作；不给迁移表、DDL 或修改题目/健康的权限。协调器授权白名单位于 `scripts/deploy_database.py`，HTTPS 验收的 `scripts/production_acceptance.py` 中 `database_roles()` 复用同一表权限清单。部署迁移增加表时必须复核权限；不能给日常 Web 超级用户或 owner 凭证。`APP_STATIC_ROOT` 可指定收集静态资源的目标，生产容器继续使用镜像内只读资源。

## 启用真实判题

基础函数推荐使用已在本机实际验证的 [CPython/WASI 后端](wasi.md)，按其指南构建、运行探针和浏览器闭环。它不需要修改 Docker daemon。以下 gVisor 配置保留为显式选择的另一策略，本机尚未验证。两者共用下面的最小数据库权限；Web 与 worker 必须使用相同 backend、制品摘要及启用状态。

### gVisor 策略

执行节点必须是专用、可销毁的 Linux 节点，不承载其他用户业务；运行时维护者参考 [gVisor 官方安装说明](https://gvisor.dev/docs/user_guide/install/) 安装受支持版本，并按 [Docker 集成文档](https://gvisor.dev/docs/user_guide/quick_start/docker/) 注册 `runsc`。本项目不自动修改宿主 Docker 配置或重启共享 daemon。记录 runsc 二进制校验、版本、Docker/内核版本、CPU 型号和 cgroup 配置，检查资源限制可用。

预检要求 Docker `info` 的 `MemoryLimit`、`SwapLimit`、`PidsLimit` 和 `CpuCfsQuota` 均为布尔值 `true`。Docker 的字段拼写是 `CpuCfsQuota`；缺失、关闭或非布尔值均拒绝，不能用其他拼写、普通容器或忽略 CPU 限额来绕过。检测到 NVIDIA 设备也不等于已经验证 GPU 执行隔离；gVisor 的 GPU 支持还涉及特定驱动和独立验收，见[官方 GPU 边界说明](https://gvisor.dev/docs/user_guide/gpu/)。

初始 judge 镜像为干净的官方 Python 3.11 image（可使用 `deploy/images.env` 的 digest），**不能使用包含数据库配置/项目源码的 Web 镜像**。guest 只看到镜像、受限临时目录与当前测试输入。预期答案保留在 worker。

在执行节点安装同版本项目依赖以及 Docker CLI。为 worker 创建数据库角色，仅授予：

```sql
GRANT CONNECT ON DATABASE praximesh TO praximesh_judge;
GRANT USAGE ON SCHEMA public TO praximesh_judge;
GRANT SELECT, UPDATE ON core_submission TO praximesh_judge;
GRANT SELECT ON core_problemversion TO praximesh_judge;
GRANT SELECT, INSERT, UPDATE ON core_workerhealth TO praximesh_judge;
```

不授予 auth/session/profile/draft、learning 或 community 表权限。worker 使用独立 APP_SECRET_KEY；它不需要 Web 会话密钥。数据库网络只允许所需连接。凭证只在控制器环境中，绝不传入 guest。以上角色已在本地真实 PostgreSQL 实例验证任务操作成功、auth_user 读取被拒绝；部署到生产仍须复验网络和实际权限。

```sh
# 通过受保护的环境文件配置 DATABASE_URL、生产设置以及下列参数。
export JUDGE_BACKEND=gvisor
export JUDGE_IMAGE='python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15'
export JUDGE_RECEIPT='/srv/praximesh/var/sandbox-receipt.json'
docker pull "$JUDGE_IMAGE"
# worker 停止时进行检查，避免把其他正在运行的评测误报为遗留容器。
uv run python manage.py sandbox_check
RUN_SANDBOX_TESTS=1 uv run pytest -m sandbox -q
# 参考解、错误变体、全部隔离探针、安全复核通过后再启用：
export JUDGE_ENABLED=1
uv run python manage.py judge_worker --worker-id judge-1
```

Web 配置同一 `JUDGE_IMAGE` 与 `JUDGE_ENABLED=1`。worker 需 24 小时内的匹配检查记录；过期或任何执行/清理异常会停止接收工作。工作节点启动配置示例见 `deploy/praximesh-worker.service.example`。重新运行检查前先停 worker，不伪造/手改 receipt。修改判题策略或镜像后必须重测。

完整浏览器判题验收：在测试实例和专用 worker 都可用时设置 `BROWSER_JUDGE=1`，运行 `tests/browser_smoke.py`。它必须分别收到真实正确解通过和错误解失败，不能注入假状态。该流程已在 WASI 后端实际通过；gVisor 因本机无 runsc 仍未执行。可用 `scripts/browser_acceptance.py --judge` 创建并清理隔离测试实例。

## 监控与故障处置

`GET /health/`：数据库可用时 HTTP 200；`evaluation` 单独表示判题是否可接收任务。内容服务正常而 judge 不可用是允许的降级。`gunicorn.conf.py` 的访问日志只记录 HTTP 方法、路径、状态和耗时；worker 记录任务 UUID 和结果类型，不记录源代码、密码、测试或数据库 URL。代理示例不启用访问日志，若运营需要 IP/请求头日志，须先明确脱敏和保留政策。

至少监测：HTTP 5xx/延迟、数据库连通性、worker 心跳年龄、排队数与最老任务等待、平台故障率、执行节点资源、遗留容器。阈值需依据试运行实测，当前没有可信的公网容量数字。

协调器部署的定时维护使用同一配置和状态目录，每分钟由可信部署账户执行：

```sh
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state maintain
```

它与发布共享文件锁和数据库会话锁，忙时可在下个周期重试；服务和 timer 示例见部署指南。不要让另一个后台 `manage.py maintain` 绕过协调锁写数据库。独立运行、未使用协调器的实例才直接调度该管理命令。

维护将租约过期任务/等待超过 10 分钟任务记为平台故障，清理到期会话/限流桶。worker 活跃时也回收过期任务；**worker 全停时仍必须安排维护**，避免用户一直看到排队。

协调器部署发生中断时先查看 `deploy.py status` 与私有日志，再使用相同配置的 `recover`；状态记录不是实时健康证明。独立 gVisor 实例故障处置时，先关闭评测入口、停 worker，再按 `praximesh.judge=true` 标签逐个核对和清理自己的容器，修复后重新进行隔离/功能检查。不要使用全局 prune。数据库任务因 lease token 校验不会被迟到 worker 覆写。

## 备份与恢复

本地 SQLite 不能只复制正在写入的文件。使用在线备份 API：

```sh
mkdir -m 700 -p /private/backup-directory
uv run python manage.py backup_sqlite /private/backup-directory/praximesh-UNIQUE.sqlite3
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
