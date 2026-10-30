# 部署指南：选方案、启动与维护

部署操作说明统一维护在本目录。先根据下表选择一种方式，再沿对应手册执行。所有命令默认在仓库根目录运行。

## 选哪种方式

| 方式 | 适用场景 | 组成与数据 | 真实评测与 AI | 操作入口 |
| --- | --- | --- | --- | --- |
| **Docker Compose 单机自部署** | 自己使用、内网体验或小规模使用，希望少配置、通过服务器 IP 做题 | Web + worker；SQLite 命名卷；默认 HTTP `0.0.0.0:8000` | 默认七种 CPU/WASI 评测；可选本地 CPU 模型或已有 HTTPS 模型服务 | [Compose 完整操作](compose.md) |
| **发布协调器单机部署** | 需要域名/HTTPS、PostgreSQL、分离数据库权限、受控升级和失败恢复的长期站点 | Caddy + 固定 Web/worker 镜像 + PostgreSQL；私有配置、部署状态与备份 | worker 通过真实检查后开放评测；AI 使用预先配置的 HTTPS 服务 | [生产部署完整操作](production.md) |
| **源码开发环境** | 修改应用、调试页面、编写内容或开发评测器 | Python/uv + Django 开发服务器；默认仓库内 SQLite；回环访问 | 默认不启动评测或 AI；按需单独配置 WASI worker 和模型 | [下方源码开发](#development) |

**想直接用起来，选 Compose。** 想同时体验本地 AI，给同一方案加上 `compose.ai.yaml`。有正式数据库、HTTPS 和受控升级要求时，选择发布协调器；它目前同样是单主机方案，没有交付多机高可用部署。

各方式沿用同一套账号、内容版本、学习、社区、组织、项目和挑战功能。主要差别在服务启动、数据存储、网络配置与升级保障。七种真实评测均为 CPU/WASI；**目前任何方案都不提供 GPU 评测**。

## Compose：直接做题和使用可选 AI

在 Linux x86_64 主机准备 Docker Engine、Docker Compose 和构建时可用的下载网络。无需宿主 Python、预先构建的 WASI 或模型服务。基础运行建议预留 3 GiB 可用内存，带本地 AI 建议 6 GiB；首次构建还需额外磁盘、下载及编译时间。

```sh
# Web + 真实 CPU 评测
docker compose up --build -d

# Web + 真实 CPU 评测 + 本地 CPU AI，选择这一组合时执行本行
docker compose -f compose.yaml -f compose.ai.yaml up --build -d
```

打开 `http://服务器IP:8000`。注册后进入 Practice →「只累加正数」，运行公开样例，再正式提交。Web 先就绪，worker 完成隔离检查后才接受评测；`/health/` 中的 `evaluation` 应为 `available`。使用 AI 时，后续 `ps`、`logs`、`stop`、`down` 和 `up` 都沿用这两个 `-f` 参数。

首次初始化会导入示例目录；已有数据库只迁移，不重新导入。账号、会话、草稿和成绩保存在 `app-data` 卷；模型令牌另存在 `ai-credentials` 卷。`down` 保留数据，`down -v` 会删除卷。

端口/代理配置、服务管理、模型接入、备份更新及排障见 [Compose 完整操作](compose.md)。当前默认 Compose 是 SQLite/HTTP 配置，启动器拒绝 production 模式及 `DATABASE_URL`；给它添加 PostgreSQL 连接变量不能完成生产部署转换。

## 发布协调器：PostgreSQL、HTTPS 与受控升级

这一方案适合愿意维护专用主机、数据库、域名、配置密钥、备份及服务监管的部署者。当前没有对应的一键生产 Compose 文件，安装入口是 `scripts/deploy.py`。

完整顺序在 [生产部署手册](production.md)：

1. 准备经过审查的控制器及锁定 Python 环境、Caddy、专用 PostgreSQL 和网络。
2. 获得经验证的发布目录及独立确认的摘要；目录包含 `release.json`、`source.tar.gz`、`images.tar`。自行制作时再读[发布包构建](releases.md)。
3. 首次 `init` 前填写私有配置，包括域名/HTTPS、数据库凭证、应用密钥、是否开放注册及可选 AI；初始化后配置受摘要约束。
4. 启动受监管的代理，通过 `apply` 安装或升级，首次导入示例内容才加 `--initial-content`。
5. 持续监控并安排维护、异机备份和恢复演练。中断时使用同一配置、状态目录的 `status` / `recover`。

协调器在维护窗口排空任务、备份/恢复到候选库、迁移、验证真实正确与错误提交，再开放服务。升级期间会返回维护页；候选可能接收用户写入后，恢复保留当前库，避免回退旧快照丢失新写入或恢复已删除账号。

固定镜像与本地 HTTPS 升级已有验收记录；目标主机的公有证书续期、容量和服务监管仍需现场验证。公开运营及项目许可证尚待维护者决定，托管 CI/CD 尚无服务端验收记录，范围见[需求追踪](../requirements.md)。

<a id="development"></a>

## 源码开发：修改代码与内容

在独立的 Linux x86_64 开发环境准备 uv；仓库锁定 Python 3.11 及依赖。以下初始化命令用于**新的开发数据库**：

```sh
uv sync --frozen
.venv/bin/python manage.py migrate
.venv/bin/python manage.py content_check
.venv/bin/python manage.py content_import --translations content/en.json
.venv/bin/python manage.py project_import content/projects.json
.venv/bin/python manage.py challenge_import content/challenges.json
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

打开 `http://127.0.0.1:8000`。数据库默认是仓库内的 `db.sqlite3`，与 Compose 的命名卷分开；开发服务器仅用于回环访问。已有服务使用固定虚拟环境，不对它运行 `uv sync` 或可能隐式同步依赖的命令。

默认可浏览、编辑和保存草稿。需要真实评测时按 [WASI 构建与本地判题](wasi.md)配置可信 bundle、摘要和检查记录，在同一开发数据库上启动 Web 与独立 worker。模型按 [AI 配置](ai.md)启用；单独启动开发 Web 不会自动启动这两个服务。

`content_import` 会撤回官方全量包中缺失的内容，是明确的内容发布动作；已有站点不可用示例包覆盖自有目录。示例挑战使用固定的 2026 年 UTC 时间窗口，过期不会自动延期。内容维护见[内容规范](../content.md)和[挑战指南](../challenges.md)，测试与贡献见 [CONTRIBUTING](../../CONTRIBUTING.md)。

## AI 如何选择

| 选择 | 何时使用 | 支持范围与配置位置 |
| --- | --- | --- |
| 不启用模型 | 只需要真实练习、作者提示和规则学习计划 | 默认状态，不影响正式评测 |
| Compose 内置 CPU 模型 | 想直接体验 AI，当前没有模型服务 | 选择 `compose.ai.yaml`；额外下载约 640 MB 的固定 Qwen 模型，自动生成私有令牌，不开放模型端口 |
| 已有 HTTPS 模型服务 | 已有组织服务，或需要自行选择模型及资源 | Compose 在私有 `.env` 配置；协调器在首次 `init` 前配置 `application.coaching`；源码开发使用 Web 环境变量。详见 [AI 配置](ai.md) |

本地小模型的能力有限，输出可能错误。所有 AI 提示都先预览、再由用户确认发送，源码默认不发送；AI 没有正式评分权限。`local_coaching_server.py` 是有时限的模型验收工具，不是常驻生产模型服务。

## 更新、迁移与容易混淆的地方

| 操作或疑问 | 应采用的路径 |
| --- | --- |
| Compose 重建、旧页面体验版升级 | 保持原项目名、配置及数据卷，排空、停服务、备份后重建；已有内容不重导，见 [Compose 数据与更新](compose.md#数据停止与更新) |
| 给已有 Compose 加本地 AI | 沿用原项目/卷，改用带 `compose.ai.yaml` 的组合；新增模型服务和令牌卷，共用原账号及数据 |
| 发布协调器升级 | 使用新的已验证制品、匹配的控制器以及原配置/状态，执行 `apply`，不加 `--initial-content` |
| 从 SQLite 改到 PostgreSQL | 当前没有一键跨数据库迁移工具；单独设计并验证数据迁移。源码数据库、Compose 卷和协调器代际库不会自动互相导入 |
| 只运行 `docker build` 或 `release.py load` | 分别完成镜像构建或导入；不会自动启动完整服务、迁移数据库或激活站点 |
| 运行 `*acceptance.py` | 在可销毁环境做验证，脚本会清理自有服务和数据；日常站点应使用对应安装入口 |
| WASI、gVisor 是其他部署套餐吗 | 它们是代码执行后端。当前两种自部署入口使用已验证的 WASI；gVisor 路径未完成本机验收，也不代表已有 GPU 支持 |
| 页面能打开但不能评测 | 先看 `/health/` 的 `evaluation` 和 worker 日志，再按[排障手册](operations.md)检查探针、心跳、暂停和额度 |

## 按任务查阅

| 任务 | 操作入口 |
| --- | --- |
| Compose 的完整生命周期 | [compose.md](compose.md) |
| PostgreSQL/HTTPS 安装、发布、升级与中断恢复 | [production.md](production.md) |
| 监控、排障、数据库备份与恢复演练 | [operations.md](operations.md) |
| 制作、校验和导入不可变发布包 | [releases.md](releases.md) |
| 单独构建 WASI 或开发执行后端 | [wasi.md](wasi.md) |
| 研究尚未验收的 gVisor 后端 | [gvisor.md](gvisor.md)，不作为已交付部署方案 |
| 已有模型接入、协议限制及模型验证 | [ai.md](ai.md) |
| 重放临时 HTTPS 验收 | [validation.md](validation.md) |

实际交付结果另见 [Compose 验收](../acceptance-compose.md)和 [F 发布与升级验收](../acceptance-f.md)。历史记录保留当次版本与范围，不能代替新主机或新制品的检查。
