# PraxiMesh

以结构化知识为导航、以编程实践为核心的学习平台。需求基线见 [PRD.md](PRD.md)。

**阶段 A 的本地学习与真实判题闭环已验证，公开部署验收尚未完成。** 导航、教程、题库、账户、私有草稿、CPython/WASI 执行、历史与路径进度已联通；评测默认关闭，配置真实沙箱后才接收提交。当前结果与缺口见 [验收记录](docs/acceptance.md)。

## 本地体验

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)。不需要 Node 或容器即可体验非执行功能。

```sh
uv sync --frozen
uv run python manage.py migrate
uv run python manage.py content_check
uv run python manage.py content_import
uv run python manage.py runserver 127.0.0.1:8000
```

打开 http://127.0.0.1:8000 。注册后保存只显示一次的恢复码。练习台会如实提示评测暂不可用；编辑和保存私有草稿仍可使用。不要把开发服务器绑定到公网。

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run pip-audit
```

测试自动收集静态资源。普通测试会跳过 5 项需要实际沙箱的检查，SQLite 还会跳过 2 项 PostgreSQL 并发检查。完整真实判题配置、构建和一键浏览器验收见 [WASI 指南](docs/wasi.md)；跳过不能作为验证通过的证据。

生产设置的真实 Caddy/Gunicorn/PostgreSQL/WASI 验收已在临时回环部署通过，包含 HTTPS 浏览器、数据库权限、并发测量和 worker 停机降级。重放方法与边界见 [HTTPS 部署验收](docs/production-acceptance.md)；公有域名和目标生产主机仍待验收。

同源的 Web 与 WASI worker 也可构建成固定镜像发布包，校验后导入并重放容器内真实练习流程，见[发布制品指南](docs/releases.md)。当前已验证制品和镜像运行，服务激活、迁移协调与自动回滚尚未完成。

发布前可使用持久暂停/排空命令，已有任务继续处理，阅读和草稿保持可用；新提交按评测指纹交给匹配 worker。操作和首次 schema 升级边界见[部署协调指南](docs/deployment-coordination.md)。

浏览器验收（独立本地测试实例，评测关闭）：

```sh
uv sync --frozen --group browser
uv run --group browser playwright install chromium
uv run --group browser python manage.py runserver 127.0.0.1:8765 --noreload
# 另一个终端
uv run --group browser python tests/browser_smoke.py
```

脚本创建并删除自己的账号，截图在忽略目录 `test-results/`。需浏览器系统依赖，CI 用受支持的 Ubuntu。

## 项目边界

| 目录 | 职责 |
| --- | --- |
| `core/` | 知识与题目版本、私有账户数据、Web 页面与队列 |
| `judge/` | 可信结果比较、WASI 执行器和独立选择的 gVisor 适配器 |
| `content/` | JSON Schema、原创示例包、公开样例和维护测试 |
| `templates/`, `static/` | 自托管页面、样式与渐进交互 |
| `tests/` | 应用、安全、真实沙箱和浏览器检查 |
| `deploy/` | 容器、TLS 代理样例和运维脚本 |
| `docs/` | 阶段计划、RFC/ADR、威胁模型、运维和验收 |

阅读 [设计索引](docs/README.md)、[内容规范](docs/content.md)、[贡献指南](CONTRIBUTING.md)、[安全边界](SECURITY.md) 和 [部署与恢复手册](docs/operations.md)。公开发布前的授权事项见 [LICENSES.md](LICENSES.md)。
