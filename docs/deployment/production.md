# PostgreSQL / HTTPS 单机部署

[部署方案选择](README.md) · 适合需要 PostgreSQL、HTTPS 和维护窗口升级的单主机站点。

`scripts/deploy.py` 协调私有 Caddy、固定镜像和独立 PostgreSQL 代际库；`judge_admission` 是其暂停/排空基础。真实五代发布、失败迁移及中断恢复已通过回环 HTTPS 验收，范围见[验收记录](../acceptance.md)。设计见 [RFC-005](../rfcs/005-deployment-coordination.md)、[ADR-007](../adr/007-admission-coordination.md)和[ADR-008](../adr/008-deployment-generations.md)。

## 安装顺序与文件位置

这是基于 `scripts/deploy.py` 的受控部署，不使用开发服务器或默认 Compose。所有控制命令在已审查的控制器源码根目录运行；配置、状态、数据库卷和发布目录独立保存，升级时继续使用原配置与状态。

| 阶段 | 本页位置 | 必须准备或保留的东西 |
| --- | --- | --- |
| 准备制品 | [发布包准备](#发布包准备) | 三个发布文件及独立确认的清单摘要；控制器与目标版本匹配 |
| 准备主机与配置 | [部署配置](#部署配置) | PostgreSQL、两个网络、私有配置、域名/TLS、Caddy；在首次 init 前决定注册与 AI |
| 首次安装 | [激活发布包](#激活发布包) | init → 受监管的 serve-proxy → apply；需要示例目录才加 initial-content |
| 日后升级 | [激活发布包](#激活发布包) | 原配置/状态及新的可信制品；不加 initial-content；预留维护窗口和备份空间 |
| 失败恢复 | [中断、失败与恢复](#中断失败与恢复) | 原状态、token 和数据库；先 status，再 recover |
| 日常维护 | [监控与备份](operations.md) | 同一实例的 maintain、数据库备份、应用密钥及部署状态 |

## 发布包准备

已获得可信发布包的部署者无需在目标主机另行构建 WASI 或安装 Wasmtime。自行制作发布包时先按[发布包构建](releases.md)操作；默认 Compose 构建出的镜像不是协调器要求的三文件发布目录。

目标主机准备独立控制器环境：

```sh
uv sync --frozen --no-dev
.venv/bin/python scripts/release.py verify \
    --release /srv/praximesh/releases/candidate --sha256 "$RELEASE_SHA256"
```

`RELEASE_SHA256` 来自独立可信记录，不能以下载后自行计算的值代替来源验证。`apply` 会校验并导入包中的镜像；仅执行 `release.py load` 不会激活服务。以下配置中的 Caddy 可由 `scripts/fetch_caddy.py --output /absolute/path/to/caddy-2.11.7` 获取固定版本，再填写实际绝对路径。

## 部署配置

目标为专用 Linux x86_64 主机及部署账号，工具使用本账号的 Docker socket。先准备经验证的控制器源码、锁定的 Python 环境、Caddy、[发布包](releases.md)，以及专用 PostgreSQL 17 容器。数据库端口只发布到 `127.0.0.1`；容器同时接入明确命名的两个 bridge：内部 backend 网络和非内部 frontend 网络。数据库持久卷、主机磁盘/内存和服务监管由操作者配置。工具不配置共享 daemon，不接管既有数据库，不自动删除数据库、卷或备份。

在空的专用部署环境中，可按以下命令准备数据库。先把随机 owner 密码放入私有环境变量 `PRAXIMESH_PG_PASSWORD`，与后续配置中的 `owner_password` 一致；不要把真实密码写入命令或版本库。示例的内存/PID 预算需在目标负载下复验，镜像 digest 固定为当前已测版本。

```sh
docker network create --internal praximesh-backend
docker network create praximesh-frontend
docker volume create --label org.praximesh.instance=learn praximesh-postgres-data
POSTGRES_PASSWORD="$PRAXIMESH_PG_PASSWORD" docker run --detach \
    --name praximesh-postgres --restart=unless-stopped \
    --network praximesh-frontend --publish 127.0.0.1::5432 \
    --memory=1g --pids-limit=128 --env POSTGRES_PASSWORD \
    --mount type=volume,src=praximesh-postgres-data,dst=/var/lib/postgresql/data \
    postgres:17-alpine@sha256:e4cb93687b062cd25a0e8f672d85a7708235009d70d31f8a7698d9370092a9c4
docker network connect praximesh-backend praximesh-postgres
docker exec praximesh-postgres pg_isready --username postgres
```

确认 `pg_isready` 成功后再初始化协调器。保留该持久卷，禁止在升级或故障处理中用 `volume prune` 或删除卷来“重试”。控制器依赖可用 `uv sync --frozen --no-dev` 安装；WASI 运行时位于 worker 镜像，控制器无需执行学习者代码。

复制 [coordinator.json.example](../../deploy/coordinator.json.example) 为私有 `config.json`，权限设为 0600，替换全部凭证。两个应用密钥各至少 50 字符且不同；数据库 owner、Web 和 worker 凭证分开。示例的 `application.registration_open` 为 `false`，此时访客可以阅读，但注册返回 403；私有部署若需要用户自行注册，在首次 `init` 前将它设为 `true`。初始化后不能直接修改该值绕过配置摘要检查。`instance` 为本 PostgreSQL 实例内唯一的短小写标识。首次初始化产生私有站点 UUID，数据库与角色标记绑定该 UUID，另一份状态不能冒领同名实例。应用密钥与数据库凭证不会传入 WASI guest。若首次部署需要可选 AI，在 `init` 前按[AI 辅导指南](ai.md)配置 `application.coaching`；省略即保持关闭。已有实例不能直接修改配置摘要来启用服务。

例子默认只在回环 HTTP 上运行。公网使用明确的 `https://域名[:端口]` 和监听地址，验证 DNS、防火墙、证书及续期。Caddy 可管理公有证书；已有证书可设置绝对路径 `tls_cert`、`tls_key`。私有 CA 验收时另设顶层 `health_ca` 路径；生产不得关闭证书验证。状态目录及其祖先不能使用符号链接；目录 0700、文件 0600。不要把配置、数据库或状态目录提交到仓库。

```sh
# 示例路径；控制器、配置、状态与发布目录互相独立。
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state init
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state serve-proxy
```

`serve-proxy` 前台运行，应交给专用账号的服务管理器。它从已持久保存的维护/活动配置启动，不依赖 PostgreSQL 在线。提供 [proxy service](../../deploy/praximesh-proxy.service.example)、[维护 service](../../deploy/praximesh-maintain.service.example)和[timer](../../deploy/praximesh-maintain.timer.example) 示例；修改路径、检查目标主机资源和端口权限后再安装，当前工作区没有安装这些服务。Web/worker 容器使用 `unless-stopped`，旧代停止后不会在 Docker 重启时自动重新启动。

## 激活发布包

```sh
# RELEASE_SHA256 必须来自可信构建记录。
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state apply \
    --release /srv/praximesh/releases/candidate --sha256 "$RELEASE_SHA256" \
    --initial-content
```

`--initial-content` 仅限首次安装，明确导入初始内容；升级时去掉该选项，工具拒绝覆盖运行中的内容。升级会暂停新评测并排空，再显示全站维护页、停止旧进程并冻结旧数据库。它将完整备份恢复到新的所属数据库，逐表比较真实行、列定义和序列状态，之后才迁移。当前验证上限为 10 GiB 表数据，不支持额外 schema、无主键表、视图或 large objects；超出时关闭发布，需独立设计扩展与复验。

候选 Web/worker 来自同一固定发布包。worker 每次启动均重新执行真实隔离探针；协调器要求本候选 worker 在本次容器启动之后发布匹配指纹的心跳。候选验证实际运行一份正确和一份错误程序，核对执行主机、制品、策略和结果，清理自己的测试数据，再恢复入口与切换代理。

这是维护窗口部署：完整备份、验证、迁移和新探针期间站点返回 503。需预留候选库、备份及镜像的磁盘空间。发布进程不允许后台维护命令绕过它；定时维护必须使用同一配置/状态的 `deploy.py maintain`，它与发布共享文件锁和 PostgreSQL 会话锁。忙时维护可失败并在下个周期重试。

## 中断、失败与恢复

```sh
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state status
.venv/bin/python scripts/deploy.py --config /srv/praximesh/config.json \
    --state /srv/praximesh/state recover
```

状态目录的 `deployment.json` 记录当前代、操作 token、阶段、制品及恢复决策。`status` 可在数据库离线时读取它；它不是实时健康证明。不要编辑 journal 或换 token 解除暂停。配置摘要发生变化时拒绝继续；凭证轮换、迁移到另一主机或丢失 journal 需要单独审查，不可临时重建一个同名实例绕过所有权检查。

排空超时保留已接收任务和关闭的新入口，原站阅读/草稿仍可用。修复 worker 或等待租约回收后运行 `recover`。候选开放之前的迁移/启动失败，协调器恢复旧库及旧进程并再次验证；首次安装失败维持维护页。已冻结旧库、失败候选库及备份保留供诊断。

在任何候选代理配置可能持久保存之前，journal 先写入 `exposure_target`。此后即使代理请求超时、进程被杀或完成记录没有落盘，都按候选可能已接收用户写入处理：`recover` 仅保留当前候选库前向恢复，不能切回旧快照。需要回退应用时也必须另行验证新数据与旧代码的兼容性。备份恢复不是已公开数据库的通用回滚机制。

旧库和备份含个人历史数据，保留政策、账号删除对历史备份的处理、异机备份及灾难恢复由运营者明确制定。默认不自动删除可恢复证据，也不宣称同机备份可以抵御磁盘损坏。

## 生产设置与权限

使用 PostgreSQL，数据库连通性与并发需真实测试。数据库 owner 仅供受保护的迁移、备份恢复与部署检查，日常角色只取得必需的表/序列权限。协调器根据本页的私有配置生成服务环境；[app.env.example](../../deploy/app.env.example) 可用于对照生产设置字段。

- `PRAXIMESH_MODE=production` 会要求随机密钥、显式主机和 PostgreSQL。
- `DEBUG` 关闭、HTTPS 重定向、HSTS、Secure/HttpOnly 会话和 CSRF cookie；静态资源从容器内读取。
- 手工配置 TLS 代理时，使用 `deploy/Caddyfile.example` 与其导入的 `deploy/proxy.caddy`，将两者放在同一目录；协调器生成并管理专用 Caddy 配置。只有代理可达后端；代理覆盖 X-Forwarded-Proto 后才能设 `TRUST_TLS_PROXY=1`，并将实际连接后端的代理 IP 加入 `TRUSTED_PROXY_IPS`。缺少可信地址时应用拒绝启动；协议值必须恰为 `http` 或 `https`，逗号列表不能授权 HTTPS。Gunicorn 的 `forwarded_allow_ips` 保持空，由应用执行这项校验。
- 代理无条件重写 X-Real-IP，避免伪造或所有用户共享代理 IP 的额度。不受信来源的转发头始终忽略，额外的 CDN/多级代理需单独设计并验证信任链；IP 白名单不能替代后端网络访问控制。
- 公共注册默认关。维护者确认运营策略和额度后才设 `REGISTRATION_OPEN=1`。
- `JUDGE_ENABLED=0` 时关闭执行但继续提供内容、账号、草稿。
- Web 镜像不安装 Docker 客户端，不挂载 Docker socket，不需要云基础设施凭证。

当前表/序列权限以 [deploy_database.py](../../scripts/deploy_database.py) 为准。迁移增加表时同时更新控制器，不以全表或 owner 授权规避权限错误。

## CI/CD 接入

WASI 工作流构建并验收固定制品；非 PR 的成功运行保留完整 `immutable-release` artifact 七天。部署工作流 [deploy.yml](../../.github/workflows/deploy.yml) 只接受手工触发的 `apply`/`recover`，默认禁用；实际服务端运行尚未验证。维护者先配置默认分支保护、受保护的 `production` environment 和专用 `praximesh-deploy` runner，最后显式设置仓库变量 `PRAXIMESH_DEPLOY_ENABLED=true`。该 runner 不能同时运行 PR 或普通构建任务。

部署主机预先安装已审查控制器于 `/srv/praximesh/controller`、私有配置/状态及代理服务。通过可信传输将三个发布文件放入 `/srv/praximesh/releases/<manifest SHA-256>/`，并核对构建记录；工作流的输入仅是该摘要及操作。它不 checkout PR、不下载执行脚本，也不接受任意命令或路径。部署账号对 Docker 的访问等同于该实例的运维权限，应只供受信发布任务使用。

工作流避免主动取消进行中的部署；主机故障或服务端作业超时仍可能中断进程，下一次需选择 `recover`。配置和凭证始终留在主机的私有文件，工作流不打印它们。这里提供自动激活入口及恢复流程，域名、主机、受保护环境和可信制品传输仍需在实际部署目标配置。

触发与环境限制依据 [GitHub workflow_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)及[部署环境配置](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idenvironment)。服务示例使用 [systemd user units](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)，尚未在目标生产主机安装。

## 首次升级

本节仅适用于尚未包含 `0002_admission_and_policy` 的旧实例。已包含该迁移的站点直接使用前述 `apply` 流程。

迁移 `0002_admission_and_policy` 增加暂停 token 和任务的评测指纹。旧 Web/worker 不理解它，不能直接依靠新命令协调旧进程：首次升级先停止旧 Web 入口，等待旧 worker 完成队列，停止旧 worker，备份并迁移，再启动新版本。不能同时保留旧 worker。历史完成记录不改；没有指纹的遗留待处理任务不会被新 worker 猜测重跑，按已有超时规则记为平台故障。

应用和 worker 使用同一发布包，并保持部署中的文件不可变。新任务的指纹结合 backend、解释器摘要与策略代码；worker 只领取与自身启动指纹匹配的任务。解释器不变而策略变化仍产生新指纹，必须重新探测和启动 worker。

## 后续暂停与排空

通过维护通道使用当前发布包的管理命令及数据库维护凭证；不要给 Web 页面增加维护权限，不要把 owner 凭证传入判题 guest。为一次操作产生 UUID，并在私有部署记录中保存它供重试：

```sh
export PRAXIMESH_DEPLOYMENT_TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4())')"
.venv/bin/python manage.py judge_admission status
.venv/bin/python manage.py judge_admission drain \
    --token "$PRAXIMESH_DEPLOYMENT_TOKEN" --timeout 180
```

`drain` 先原子暂停新 Run/Submit，再等待所有 queued/running 任务结束；用户仍可阅读、登录、保存草稿并查看原任务结果。返回 JSON 包含暂停所有权、等待/运行数量及当前策略 worker 是否健康，不包含代码、题目测试、密码或数据库 URL。只需要关闭入口时使用 `pause`，它不等待任务结束。

同一 token 可重试，其他 token 不能夺取或解除暂停。超时、Ctrl-C 或维护进程退出后仍暂停；命令不取消任务、不伪造结果，不自动强杀 worker。先检查 worker、租约和 `incompatible_pending`，保持正常的 `maintain` 计划任务。没有健康 worker 时，排空可能超时；不能以删任务或手改结果让检查通过。

确认排空后，协调器才停止旧 worker 并处理兼容性、备份、迁移和候选验证。独立使用此命令时，不能把排空成功等同于发布成功。

## 恢复入口

管理命令失败时，协调器在私有状态目录保存 `control-failure-<UUID>.json`，文件为 0600。其中记录命令类别、退出码和脱敏后的 stdout/stderr 尾部，每路最多 64 KiB、整个文件最多 1 MiB；常规错误只给文件名定位。日志不记录命令行或环境，已知配置凭证会被替换，但它仍是仅供部署维护者读取的内部诊断，不应作为公开 CI artifact 上传。诊断无法落盘时，原有控制容器清理和恢复流程仍会执行。

候选或恢复后的版本完成实际隔离探针、健康检查和功能验证后，用原 token 恢复：

```sh
.venv/bin/python manage.py judge_admission resume \
    --token "$PRAXIMESH_DEPLOYMENT_TOKEN"
.venv/bin/python manage.py judge_admission status
```

恢复会拒绝错误 token、关闭的评测配置、失联或其他策略的 worker，以及尚存的其他策略任务。暂停本身不会让 worker 停止；不要把它当成宿主执行隔离或数据库整体只读模式。

## 复验

```sh
# PostgreSQL 并发矩阵；使用独立容器和显式数据库卷并自动清理。
.venv/bin/python scripts/postgres_tests.py -q
# 已配置真实 WASI 后，把 sandbox 回归也包含进同一次运行。
RUN_SANDBOX_TESTS=1 .venv/bin/python scripts/postgres_tests.py -q
# 最新发布包的真实排空/恢复随容器浏览器验收一起验证。
.venv/bin/python scripts/release_acceptance.py \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
# 完整部署恢复：先构建仅供验收的真实失败迁移制品。
.venv/bin/python scripts/build_failed_migration_release.py \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256" \
    --bundle "$JUDGE_WASI_BUNDLE" --bundle-sha256 "$JUDGE_WASI_SHA256" \
    --output var/releases/migration-failure
# FAILED_RELEASE_SHA256 使用上一命令输出，不得把故障制品部署到实际站点。
.venv/bin/python scripts/deploy_acceptance.py \
    --release var/releases/candidate --manifest-sha256 "$RELEASE_SHA256" \
    --failed-release var/releases/migration-failure \
    --failed-manifest-sha256 "$FAILED_RELEASE_SHA256" \
    --caddy var/tools/caddy-2.11.7
# 只复验首次 HTTPS 核心闭环、失败迁移诊断和旧库恢复时，
# 在上一条命令追加 --migration-only；此模式不运行中断/自动重启矩阵。
# 只重放自动重启期间的恢复检查时，可使用独立模式：
.venv/bin/python scripts/deploy_acceptance.py \
    --release var/releases/candidate --manifest-sha256 "$RELEASE_SHA256" \
    --caddy var/tools/caddy-2.11.7 --worker-restart-only
```

真实场景先提交有界的标准库函数，让 `drain --timeout 0` 确实超时；检查新提交 503、阅读和草稿可用，等待原任务实际 Accepted，拒绝错误 token，再恢复并执行另一份正确提交。测试自己的账号和数据库均清理，不使用注入的评测结果。

## 阶段 A 到 B1 的真实 schema 升级复验

本批已验证 `learning.0001_initial`、`community.0001_initial` 的新增表迁移、原数据保留、按实际迁移授予 Web 权限及 worker 拒绝访问。它保留旧目录，不自动发布扩充题库；新目录仍按内容审核流程显式导入。具体身份和限制见 [B1 验收](../acceptance-b1.md)。

```sh
python scripts/deploy_acceptance.py \
  --release /path/to/b1-release --manifest-sha256 TRUSTED_B1_DIGEST \
  --upgrade-only \
  --previous-release /path/to/phase-a-release \
  --previous-manifest-sha256 TRUSTED_PHASE_A_DIGEST \
  --caddy /path/to/caddy-2.11.7 \
  --artifacts test-results/deployment-upgrade
```

两个 digest 必须独立确认。上述 A→B1 调用验证该迁移组合，不能用同一版本代替，也不能与失败迁移或 worker-restart-only 模式混用。它在隔离临时实例中创建真实用户/草稿/AC，比较迁移前后的身份、数据和目录，实际检查八张新表的权限，并通过 HTTPS 多账号浏览器核验社区流程。只在全部步骤成功后记录 schema 升级通过。


## B2 到 C 的迁移与内容发布

C 增加 CPU 调用预约、博客类型、组织课程/题库和私有学习规划。B2→C 冻结制品升级已通过，证据与制品身份见 [C 验收](../acceptance-c.md)；C→D 增加挑战表的升级见 [D 验收](../acceptance-d.md)。以下保留 B2→C 的操作与复验入口。

升级前安装与 C 制品匹配、已审查的新控制器及锁定依赖，保留原配置、状态目录和站点 UUID。旧 B2 控制器的表权限白名单不包含 C 新表，不能只更换应用镜像。使用前述 `deploy.py apply` 命令和独立确认的 C 发布摘要，**不加 `--initial-content`**；协调器依次排空、备份/恢复、迁移、按实际 schema 授权并重启匹配的 Web/worker。

本次新增六项迁移：`core.0003_cpu_reservations`、`community.0002_post_blog_kind`、`organizations.0001_initial`、`organizations.0002_publication_lock`、`coaching.0001_initial`、`coaching.0002_slot_lock`。迁移保留旧提交的源码、结果和版本；既有记录成为零单位 `legacy`，不追溯扣额。新 Run/Submit 使用 `cpu-invocations-v1`，默认每账号每 UTC 日 120 单位；函数/ML/科学每个测试预约 1，完整程序预约 2，Agent 每个 episode 预约 6。这是最大调用次数预约，不是实测 CPU 秒或收费。完整资源和消费/释放规则见[专业评测设计](../rfcs/010-specialist-evaluation.md)。

C 新增 17 张表：Web 可写 10 张组织工作流表和 5 张辅导表，只读组织归属表与发布锁表；worker 不获这些新表权限。知识/题目及其他内容表仍沿用只读限制，不能通过全表授权消除权限错误。新 worker 必须在当前实例通过全部 123 项探针后才就绪，不能复用 B2 的 103 项回执。

schema 升级保留原目录，不自动发布 C 新题或改写课程、项目、题目历史版本。只有明确决定以已审查的 C 示例包整体替换本实例的官方目录时，才在独立维护窗口，用配置好**当前代 owner 数据库连接**的可信 C 代码环境执行：

```sh
.venv/bin/python manage.py content_check content/starter.json
.venv/bin/python manage.py content_import --translations content/en.json
```

这一步是显式内容发布，不是迁移的一部分；官方导入会下线其管理范围中缺失的 ID。组织包拥有的内容由归属锁保护，不受官方全量撤回影响，仍通过独立审核及 `org_package_publish` / `org_package_withdraw` 管理。自有官方目录应发布其经过审核的完整包，不要直接用示例目录覆盖。细节见[内容指南](../content.md)和[组织题库指南](../rfcs/009-organizations-and-packages.md)。

在可销毁环境中复验旧 B2→新 C，使用两个独立可信的发布摘要：

```sh
.venv/bin/python scripts/deploy_acceptance.py \
  --release /path/to/c-release --manifest-sha256 TRUSTED_C_DIGEST \
  --upgrade-only \
  --previous-release /path/to/b2-release \
  --previous-manifest-sha256 TRUSTED_B2_DIGEST \
  --caddy /path/to/caddy-2.11.7 \
  --artifacts test-results/deployment-c-upgrade
```

该演练先核对 schema 迁移、旧用户数据、旧目录和权限保留，再单独显式发布已审查 C 内容并验证新提交的真实额度预约。不能将同一发布包用于前后两代，也不能与失败迁移或 worker 重启模式混用；只有实际回执成功才算完成本次验收。

## E 到 F 的版本题目反馈升级

F 新增 `community.0003_problem_feedback`，为题目反馈增加明确版本约束；不增加表或角色权限。E→F 的独立冻结镜像、备份恢复、原数据/目录/权限保留、真实判题和 HTTPS 反馈流程已验证，身份及边界见 [F 验收](../acceptance-f.md)。本次没有自动导入新目录。

在可销毁环境中复验，使用各自独立确认的摘要：

```sh
.venv/bin/python scripts/deploy_acceptance.py \
  --release /path/to/f-release --manifest-sha256 TRUSTED_F_DIGEST \
  --upgrade-only \
  --previous-release /path/to/e-release \
  --previous-manifest-sha256 TRUSTED_E_DIGEST \
  --caddy /path/to/caddy-2.11.7 \
  --artifacts test-results/deployment-f-upgrade
```

实际站点使用前述 `deploy.py apply`，保留配置、状态目录与站点 UUID，并安装与 F 匹配的控制器；不加 `--initial-content`。此次演练不能替代目标主机的容量、公有证书或监管验收。
