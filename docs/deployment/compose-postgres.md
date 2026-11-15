# PostgreSQL 与 GPU Compose

这是固定发布版本的新安装方案。CPU/WASI 与本地 AI 已通过两轮实际启动和保留数据重建；GPU 首次完整启动、两题正式 API 已通过，重建复验因外部占用尚未完成。跨版本维护尚未交付，长期站点若需要升级和失败恢复，应选择[发布协调器](production.md)。各方案的用途统一见[方案选择](README.md)，当前证据见[独立验收](../acceptance-compose-postgres.md)。

此方案使用独立 PostgreSQL、Web、CPU/WASI worker；可选择专用 GPU worker 和本地 CPU AI。默认监听 `0.0.0.0:8000`，不提供 HTTPS。需要域名、受控版本升级和发布恢复时使用[发布协调器](production.md)。它不导入现有 SQLite 数据，也不覆盖现有站点目录。

## 首次准备

准备已核验 CPU 或[四镜像发布包](releases.md#gpu-release)、独立 Python 环境，以及具备 CPU/内存/cgroup 限额的私有 rootless Docker 和 Compose。GPU 还需明确授权的持续独占单卡及对应主机驱动。安装目录必须是新的绝对路径，不能含符号链接或冒号。

```sh
.venv/bin/python scripts/prepare_compose.py \
  --release /absolute/path/to/verified-release \
  --sha256 VERIFIED_RELEASE_JSON_SHA256 \
  --state /absolute/path/to/new-installation \
  --docker-socket "$XDG_RUNTIME_DIR/docker.sock"
```

加 `--ai` 选择本地 CPU 模型；加 `--gpu GPU-完整物理UUID` 选择 GPU。GPU 准备会读取真实设备元数据并创建原私有锁及任务记录目录，不能在其他账号使用此卡时执行。`--port` 改端口，`--bind 127.0.0.1` 可仅绑定回环。默认在新库导入示例目录；`--empty` 只建结构，供维护者另行发布内容。当前默认示例包仍不发布 CUDA 候选题。

准备核对完整归档、镜像角色、原 daemon 与配置协议，导入固定镜像并生成私有凭据。它不启动服务、不运行评测、不产生 GPU 就绪。完成后返回可直接执行的 `start_command`，并生成固定项目、daemon 和实际配置的 `compose.sh`。解析后的私有 `compose.json` 包含凭据，不要公开提交或打印其完整内容。

## 启动与同版本管理

例如安装目录为 `/absolute/path/to/new-installation`：

```sh
sh /absolute/path/to/new-installation/compose.sh up --build -d
```

该脚本调用真实 Docker Compose，已选择相应 GPU/AI 组合并固定镜像与配置；宿主同名环境变量不会覆盖它们。打开 `http://服务器IP:8000`。数据库初始化成功后才启动应用和 worker；CPU 与 GPU 各自完成完整检查并发布新鲜状态后才接受相应提交。Web 可访问不表示 GPU 已就绪。

沿用同一命令前缀，将末尾替换为 `ps`、`logs --tail 100`、`stop` 或 `up -d`。同版本重启保留原数据库与凭据，不重新导入目录。查看 `http://服务器IP:8000/health/`：`evaluation` 和 `gpu_evaluation` 分别说明 CPU 与 GPU 的当前可用性。GPU worker 使用原配置和任务记录，每次重新启动仍需完整检查；失败保留原状态，不能删除锁或任务记录来强行启动。

GPU 因外部占用而退出时，先协调独占设备并查看原 `gpu-worker` 日志。确认原实例已经退出、设备重新空闲后，可用 `sh /absolute/path/to/new-installation/compose.sh start gpu-worker` 重启原容器；随后仍须等待完整检查与健康状态。不要在它仍运行时重复启动，或通过重新创建安装目录绕过失败。

安装目录保存私有角色配置、独立初始化标记、冻结源码和可选 GPU 状态；PostgreSQL 数据与 AI 凭据使用带安装标识的命名卷。全部需要纳入备份。`down` 保留卷；`down -v` 会删除数据卷，后续初始化将因原绑定缺失而拒绝新建，不能用它作为恢复方式。

## 版本变化与恢复边界

当前入口只准备一个固定发布版本。再次准备必须提供原目录和完全相同的选项；不同发布包、设备、密钥、daemon 或目录身份会被拒绝。不要手工编辑生成的 `.env`、`installation.json` 或初始化标记来绕过检查。数据库元数据和宿主标记共同区分新安装与已绑定安装，丢失原库不能自动重建为空站点。

跨版本维护、备份恢复工具和 GPU 重建验收仍在实施，不能将已通过的数据库组件、首次启动或协调器验收算作这些步骤通过。当前设计与分批证据见 [RFC-042](../rfcs/042-complete-gpu-compose-site.md)、[ADR-096](../adr/096-postgres-compose-application-entry.md)和[ADR-097](../adr/097-compose-postgres-bootstrap.md)。
