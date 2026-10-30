# Docker Compose 单机部署

[部署方案选择](README.md) · 适合个人或小规模自部署；首次启动、AI、端口、数据和日常命令都在本页。

默认入口包含 Web、七种真实 CPU/WASI 评测器、账号和学习记录、项目、社区、组织、挑战及中英界面。可选覆盖文件增加本地 CPU AI 辅导。GPU 评测尚未实现；详见[需求追踪](../requirements.md)。

## 启动与使用

需要 Linux x86_64 的 Docker Engine、Docker Compose 和可访问固定依赖来源的构建网络。宿主无需 Python、WASI、模型服务或 Docker socket 挂载。基础运行建议留出 3 GiB 可用内存，带 AI 建议 6 GiB；首次构建还需编译工具链、约数 GB 磁盘与下载时间，后续复用 Docker 构建缓存。构建继承宿主的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，不将代理写进运行镜像；代理必须能从构建容器访问。

```sh
# Web + 真实评测
docker compose up --build -d

# 同时使用本地 CPU AI 辅导（首次额外下载约 640 MB 模型）
docker compose -f compose.yaml -f compose.ai.yaml up --build -d
```

打开 `http://服务器IP:8000`，注册后进入 Practice →「只累加正数」，运行样例，再提交正确或错误代码查看结果。AI 入口在题目页：先预览发送内容，再明确确认。组织、社区及学习功能共用原账号；管理命令使用 `docker compose exec web python -m core.selfhost manage COMMAND`，例如 `createsuperuser`。

Web 初始化后即可阅读。worker 首次启动需要完成全部真实隔离探针；`healthy` 后才接受提交。查看：

```sh
docker compose ps
docker compose logs -f worker
curl http://127.0.0.1:8000/health/
```

就绪响应包含 `"evaluation": "available"`。探针失败会保留不可用状态并输出具体失败；不跳过检查、不生成假记录，也不在宿主直接运行学员代码。worker 无网络，解释器只通过 WASI 能力接口执行，平台保留预期结果。每次重启重新检查；24 小时 receipt 过期后 worker 退出，Compose 重启并重跑检查，期间新提交关闭。

默认绑定 `0.0.0.0:8000`。端口可用 `PRAXIMESH_PORT=8001` 调整；仅本机使用可设 `PRAXIMESH_BIND=127.0.0.1`。显式设置 `APP_ALLOWED_HOSTS` 时，保留 `127.0.0.1` 供健康检查。部署配置写入本机 `.env`，不要提交。基础配置关闭 Django 调试详情。

常用配置如下；设置后用原来的 Compose 文件组合重新创建服务：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `PRAXIMESH_BIND` | `0.0.0.0` | 宿主监听地址；同机开发可改为 `127.0.0.1` |
| `PRAXIMESH_PORT` | `8000` | 宿主端口，容器内始终为 8000 |
| `APP_ALLOWED_HOSTS` | `*` | 可选的逗号分隔主机/IP 白名单，包含健康检查使用的 `127.0.0.1` |
| `JUDGE_DAILY_CPU_UNITS` | `120` | 每账号每 UTC 日的最大调用预约单位，不是实际 CPU 秒 |
| `COACHING_DAILY_LIMIT` | `5` | 启用 AI 后的每日请求次数 |

选择内置 AI 时，例如查看状态与模型日志：

```sh
docker compose -f compose.yaml -f compose.ai.yaml ps
docker compose -f compose.yaml -f compose.ai.yaml logs -f tutor
```

## 数据、停止与更新

既有 `app-data` 卷及 `preview-secret.key` 文件名保持，原页面体验版可直接升级；账号、会话、草稿、目录和历史不重置。新数据库自动导入完整示例；旧数据库仅迁移，不重导内容。不要使用 `down -v` 日常停止。

保持创建实例时的 Compose 项目名与 `.env`。换仓库目录或改变 `-p` 项目名可能选择另一组卷，看起来像新站点；原卷的数据不会自动搬过去。

| 数据 | 容器位置 | 需要保留的内容 |
| --- | --- | --- |
| 应用数据卷 `app-data` | Web/worker 的 `/data` | `praximesh.sqlite3`、`preview-secret.key` 及卷内完整状态 |
| 内置模型凭证卷 `ai-credentials` | Web/tutor 的 `/ai` | 自动生成的私有模型令牌；不授予 worker |
| 操作者配置 | 宿主私有 `.env` | 原端口/主机配置，以及显式应用密钥或外部模型凭证（若有） |

默认自动生成的实例密钥文件为 `0600`。若显式提供 `APP_SECRET_KEY`，继续使用并私密备份该原值；它未必保存在卷内文件中。不可通过换密钥“恢复”数据库，否则原会话会失效。已有库缺少某些译文、项目或挑战时，由维护者审查后显式发布，不能靠删除数据库重新初始化补齐。

```sh
docker compose down
docker compose up -d
```

使用 AI 时，后续启动、停止和日志命令都带 `-f compose.yaml -f compose.ai.yaml`，模型令牌在独立 `ai-credentials` 卷中保留。模型容器没有数据库卷；worker 没有模型令牌。模型只监听 Web 所在网络命名空间的 `127.0.0.1:8081`，不向宿主发布端口。已固定的模型和 llama.cpp 源码在构建时检查摘要，运行不下载新权重。

更新按以下顺序操作：

1. 用原 Compose 文件组合构建新镜像；此时旧容器继续运行。
2. 安排维护窗口，通过 `python -m core.selfhost manage judge_admission drain --token UUID --timeout 180` 暂停新评测并等待已有任务结束。该命令在 Web 容器中执行；先生成并私密保存本次操作的 UUID，超时后仍保持暂停，不能换 token 重试。
3. 停止整套服务，备份完整 `app-data`、启用时的 `ai-credentials` 和宿主私有配置。不要复制仍在写入的 SQLite 主文件；备份放在仅维护者可读的目录。
4. 用原项目名、配置和卷运行 `up --build -d`，等待 worker 真实自检完成，核对账号、目录、草稿和历史。
5. 用同一 UUID 执行 `python -m core.selfhost manage judge_admission resume --token UUID`，确认 `/health/` 的 `evaluation` 为 `available`。旧页面体验版原本没有评测任务，首次启用 worker 时无需暂停/恢复步骤。

`UUID` 是本次操作生成并保存的值，不是直接照抄的字符串。暂停只关闭评测，直到停止 Web 前仍可能有草稿等写入；一致备份须在服务停止后完成。恢复演练使用独立项目和空卷，保留容器文件的 UID/GID `65532:65532`、数据库及密钥的私有权限，再核对真实数据；日常恢复不删除原卷。

上述步骤由操作者执行，Compose 不自动完成排空、备份或失败回滚。需要自动协调 PostgreSQL、HTTPS 和受控升级时，选择[发布协调器](production.md)。通用备份与故障处理见[运维手册](operations.md)。

## 使用已有模型服务

不使用 `compose.ai.yaml`，在私有 `.env` 中设置：

```dotenv
COACHING_ENABLED=1
COACHING_ENDPOINT=https://your-tutor.example/v1/chat/completions
COACHING_MODEL=your-model
COACHING_TOKEN=your-private-service-token
COACHING_PROVIDER_DESCRIPTION=填写实际服务方、发送范围和数据保留说明
```

运行基础 Compose 命令重新创建 Web。仅 Web 接收这些配置，worker 不接收。私有 CA 可用 `COACHING_CA_PEM`；配额、并发和响应上限沿用[AI 辅导指南](ai.md)。外部端点必须 HTTPS，本地回环例外仅用于已选择的模型覆盖文件。AI 故障不影响正式评测；未配置 AI 时仍有作者提示、规则计划和学习证据。

本地小模型只用于辅助体验，可能给出错误建议；不能据它改写正式成绩。模型使用既有固定的 Qwen3-0.6B-Q8_0 来源和 llama.cpp 版本，身份与质量限制见[模型指南](ai.md)。

## 验证

开发环境可使用独立项目重放，不操作当前实例的数据卷：

```sh
.venv/bin/python scripts/compose_acceptance.py
.venv/bin/python scripts/compose_acceptance.py --ai --browsers
```

第二条需要独立开发环境的 judge/browser 依赖组及 Chromium；可用 `.venv/bin/playwright install chromium` 安装，若已有独立缓存则设置 `PLAYWRIGHT_BROWSERS_PATH` 指向它。脚本先确认浏览器能启动再创建测试服务；`--ai` 会自动包含浏览器检查。验收覆盖真实正确/错误提交、Run/Submit、全部 CPU 评测器、worker 离线关闭提交、重建后的数据与历史保留；浏览器检查包括注册、私有草稿、社区、项目、中英界面、类/API 和真实 AI 提示。结果在忽略目录 `test-results/px-compose-*/`，测试结束清理其自有容器、卷及网络。

当前结果、原始失败与修复以及验证边界见 [Compose 验收记录](../acceptance-compose.md)。
