# Docker Compose 单机部署

默认入口包含 Web、七种真实 CPU/WASI 评测器、账号和学习记录、项目、社区、组织、挑战及中英界面。可选覆盖文件增加本地 CPU AI 辅导。GPU 评测尚未实现；详见[需求追踪](requirements.md)。

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

## 数据、停止与更新

既有 `app-data` 卷及 `preview-secret.key` 文件名保持，原页面体验版可直接升级；账号、会话、草稿、目录和历史不重置。新数据库自动导入完整示例；旧数据库仅迁移，不重导内容。不要使用 `down -v` 日常停止。

```sh
docker compose down
docker compose up -d
```

使用 AI 时，后续启动、停止和日志命令都带 `-f compose.yaml -f compose.ai.yaml`，模型令牌在独立 `ai-credentials` 卷中保留。模型容器没有数据库卷；worker 没有模型令牌。模型只监听 Web 所在网络命名空间的 `127.0.0.1:8081`，不向宿主发布端口。已固定的模型和 llama.cpp 源码在构建时检查摘要，运行不下载新权重。

更新前停止接收操作并等待已有评测结束，停止整套服务，备份 `app-data` 卷后重建。不要直接删除数据卷来修复启动问题。单机 SQLite 适合个人或小规模自部署；这条命令不自动执行生产发布的排空、备份和失败回滚。长期 PostgreSQL、HTTPS、最小数据库角色及受控升级按[发布与部署协调](deployment-coordination.md)执行。

## 使用已有模型服务

不使用 `compose.ai.yaml`，在私有 `.env` 中设置：

```dotenv
COACHING_ENABLED=1
COACHING_ENDPOINT=https://your-tutor.example/v1/chat/completions
COACHING_MODEL=your-model
COACHING_TOKEN=your-private-service-token
COACHING_PROVIDER_DESCRIPTION=填写实际服务方、发送范围和数据保留说明
```

运行基础 Compose 命令重新创建 Web。仅 Web 接收这些配置，worker 不接收。私有 CA 可用 `COACHING_CA_PEM`；配额、并发和响应上限沿用[AI 辅导指南](local-coaching.md)。外部端点必须 HTTPS，本地回环例外仅用于已选择的模型覆盖文件。AI 故障不影响正式评测；未配置 AI 时仍有作者提示、规则计划和学习证据。

本地小模型只用于辅助体验，可能给出错误建议；不能据它改写正式成绩。模型使用既有固定的 Qwen3-0.6B-Q8_0 来源和 llama.cpp 版本，身份与质量限制见[模型指南](local-coaching.md)。

## 验证

开发环境可使用独立项目重放，不操作当前实例的数据卷：

```sh
.venv/bin/python scripts/compose_acceptance.py
.venv/bin/python scripts/compose_acceptance.py --ai --browsers
```

第二条需要独立开发环境的 judge/browser 依赖组及 Chromium；可用 `.venv/bin/playwright install chromium` 安装，若已有独立缓存则设置 `PLAYWRIGHT_BROWSERS_PATH` 指向它。脚本先确认浏览器能启动再创建测试服务；`--ai` 会自动包含浏览器检查。验收覆盖真实正确/错误提交、Run/Submit、全部 CPU 评测器、worker 离线关闭提交、重建后的数据与历史保留；浏览器检查包括注册、私有草稿、社区、项目、中英界面、类/API 和真实 AI 提示。结果在忽略目录 `test-results/px-compose-*/`，测试结束清理其自有容器、卷及网络。

当前结果、原始失败与修复以及验证边界见 [Compose 验收记录](acceptance-compose.md)。
