# PostgreSQL 与 GPU Compose

当前为开发分支中的新安装方案，实际整站 Compose 验收与版本维护尚未完成。已有站点继续使用原方案；[方案选择](README.md)比较默认 SQLite Compose 与发布协调器。

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

沿用同一命令前缀，将末尾替换为 `ps`、`logs --tail 100`、`stop` 或 `up -d`。同版本重启保留原数据库与凭据，不重新导入目录。GPU worker 使用原配置和任务记录，每次重新启动仍需完整检查；失败保留原状态，不能删除锁或任务记录来强行启动。

安装目录保存私有角色配置、独立初始化标记、冻结源码和可选 GPU 状态；PostgreSQL 与 AI 凭据使用带安装标识的命名卷。全部需要纳入备份。`down` 保留卷；`down -v` 会删除数据卷，后续初始化将因原绑定缺失而拒绝新建，不能用它作为恢复方式。

## 版本变化与恢复边界

当前入口只准备一个固定发布版本。再次准备必须提供原目录和完全相同的选项；不同发布包、设备、密钥、daemon 或目录身份会被拒绝。不要手工编辑生成的 `.env`、`installation.json` 或初始化标记来绕过检查。数据库元数据和宿主标记共同区分新安装与已绑定安装，丢失原库不能自动重建为空站点。

跨版本维护、备份恢复工具和完整 GPU Compose 仍在实施，不能将已通过的数据库组件或协调器验收算作本方案通过。当前设计与分批证据见 [RFC-042](../rfcs/042-complete-gpu-compose-site.md)、[ADR-096](../adr/096-postgres-compose-application-entry.md)和[ADR-097](../adr/097-compose-postgres-bootstrap.md)。
