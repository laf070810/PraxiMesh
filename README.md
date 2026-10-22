# PraxiMesh

以结构化知识为导航、以编程实践为核心的学习平台。需求基线见 [PRD.md](PRD.md)。

**已交付可自部署的阶段 A 核心，尚未公开上线。** 导航、教程、题库、账户、私有草稿、CPython/WASI 执行、历史与路径进度已联通；评测默认关闭，配置真实沙箱后才接收提交。当前结果与缺口见 [验收记录](docs/acceptance.md)。

完整阶段 B/C 正在实施，范围见[需求追踪](docs/requirements.md)。本批将题库扩充至 18 题、3 条路径，并新增私有收藏和笔记、阅读记录、实践证据与复习建议，以及需要主动送审的社区讨论、题解、评论、举报和申诉。本批已通过真实 PostgreSQL/WASI、浏览器与冻结镜像升级验证，范围见 [B1 验收](docs/acceptance-b1.md)。

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

测试自动收集静态资源。普通测试会跳过需要实际沙箱的场景，SQLite 还会跳过 PostgreSQL 专属检查。完整真实判题配置、构建和一键浏览器验收见 [WASI 指南](docs/wasi.md)；跳过不能作为验证通过的证据。

生产设置的真实 Caddy/Gunicorn/PostgreSQL/WASI 验收已在临时回环部署通过，包含 HTTPS 浏览器、数据库权限、并发测量和 worker 停机降级。重放方法与边界见 [HTTPS 部署验收](docs/production-acceptance.md)；公有域名和目标生产主机仍待验收。

同源的 Web 与 WASI worker 可构建成固定镜像发布包，见[发布制品指南](docs/releases.md)。单主机协调器已通过五代真实回环 HTTPS 演练：排空、维护窗口、候选数据库备份恢复、真实 DDL 失败回滚、实际判题检查、代理激活及中断恢复。成功替换使用同一 schema，不能据此保证任意未来迁移兼容。

开放前的失败可恢复旧代；开放意图持久保存后，恢复保留当前数据库，避免丢失用户写入或恢复已删除账号。操作、首次 schema 升级和恢复边界见[部署协调指南](docs/deployment-coordination.md)与[ADR-008](docs/adr/008-deployment-generations.md)。部署工作流需手工触发且默认禁用；托管 CI、实际生产目标及公有证书尚未验收。

浏览器验收（独立本地测试实例，评测关闭）：

```sh
uv sync --frozen --group browser
uv run --group browser playwright install chromium
uv run --group browser python scripts/browser_acceptance.py
```

脚本启动独立临时数据库和回环服务，创建并删除测试账号，结束清理进程及数据库。截图在忽略目录 `test-results/`。需浏览器系统依赖，CI 用受支持的 Ubuntu。

## 项目边界

| 目录 | 职责 |
| --- | --- |
| `core/` | 知识与题目版本、私有账户数据、Web 页面与队列 |
| `learning/` | 私有收藏、笔记、阅读记录及实践证据 |
| `community/` | 主动送审的讨论、题解、评论、举报与申诉 |
| `judge/` | 可信结果比较、WASI 执行器和独立选择的 gVisor 适配器 |
| `content/` | JSON Schema、原创示例包、公开样例和维护测试 |
| `templates/`, `static/` | 自托管页面、样式与渐进交互 |
| `tests/` | 应用、安全、真实沙箱和浏览器检查 |
| `deploy/` | 容器、TLS 代理样例和运维脚本 |
| `docs/` | 阶段计划、RFC/ADR、威胁模型、运维和验收 |

阅读 [设计索引](docs/README.md)、[内容规范](docs/content.md)、[贡献指南](CONTRIBUTING.md)、[安全边界](SECURITY.md) 和 [部署与恢复手册](docs/operations.md)。公开发布前的授权事项见 [LICENSES.md](LICENSES.md)。
