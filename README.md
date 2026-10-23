# PraxiMesh

以结构化知识为导航、以编程实践为核心的学习平台。需求基线见 [PRD.md](PRD.md)。

**当前交付范围为可本地验证的自部署版本。公开运营、项目许可证和真实 GPU 验收留待后续。** 评测默认关闭，配置并通过真实沙箱门槛后才接收提交；AI 辅导也默认关闭。

当前提供 26 道题、50 个知识节点、6 条学习路径及四阶段工程项目，支持 Python 函数、JavaScript 函数、Python 完整程序、有状态类/API，以及 CPU 上的 ML 训练、加权拟合和受控工具 Agent 实践。账户、源码、草稿、笔记、学习计划和专业成绩默认私有。

社区包含主动送审的题解、讨论和博客；公开主页与普通题型榜单需要本人选择分享。组织课程固定题目版本，第三方题库经过独立审核并由维护者发布。学习规划和综合证据不依赖模型；可选 AI 提示需逐次预览并确认输入，不能修改正式成绩。

阶段 A/B 已通过真实 PostgreSQL/WASI、HTTPS 浏览器与冻结镜像升级验收，见 [B1](docs/acceptance-b1.md) 和 [B2](docs/acceptance-b2.md)。C 的专业评测、组织与智能学习也已完成本地联合验收，范围见 [C 验收](docs/acceptance-c.md)。完整筛选、私有成就及挑战活动已通过真实 HTTPS、冻结镜像和 C→D 升级验收，见 [D 验收](docs/acceptance-d.md)；Docker 能力预检修正见 [D3 验收](docs/acceptance-d3.md)；有状态类/API及当前固定镜像已完成本地联合验收，见 [E 验收](docs/acceptance-e.md)与[接口指南](docs/object-api-contract.md)；全部范围与外部待办见[需求追踪](docs/requirements.md)。

## 本地体验

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)。不需要 Node 或容器即可体验非执行功能。

```sh
uv sync --frozen
uv run python manage.py migrate
uv run python manage.py content_check
uv run python manage.py content_import --translations content/en.json
uv run python manage.py project_import content/projects.json
uv run python manage.py challenge_import content/challenges.json
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

同源的 Web 与 WASI worker 可构建成固定镜像发布包，见[发布制品指南](docs/releases.md)。单主机协调器的基线通过五代真实回环 HTTPS 演练：排空、维护窗口、候选数据库备份恢复、真实 DDL 失败回滚、实际判题检查、代理激活及中断恢复。后续另行验证了 B1→B2、B2→C 与 C→D 的实际 schema 升级和旧数据保留；未来迁移仍须独立验收。

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
| `learning/` | 私有收藏、笔记、阅读记录、实践证据及可核验成就 |
| `challenges/` | 限时活动、不可变任务绑定和自愿加入的私有进度 |
| `community/` | 主动送审的讨论、题解、博客、评论、举报与申诉 |
| `organizations/` | 私有组织、角色邀请、版本课程和有归属范围的第三方题库 |
| `coaching/` | 自选目标、固定版本计划、多维证据与可选的私有 AI 提示 |
| `practice_api/` | 有效期与用途受限的访问令牌、版本化公开任务和本人提交 API |
| `projects/` | 维护者发布的不可变项目计划、固定题目版本与私有进度 |
| `showcase/` | 自愿公开主页、可撤回结果和可比较的分组榜单 |
| `localization/`, `locale/` | 中英界面、精确源版本译文与明确原文回退 |
| `judge/` | 可信结果比较、WASI 执行器和独立选择的 gVisor 适配器 |
| `content/` | JSON Schema、原创示例包、公开样例和维护测试 |
| `templates/`, `static/` | 自托管页面、样式与渐进交互 |
| `tests/` | 应用、安全、真实沙箱和浏览器检查 |
| `deploy/` | 容器、TLS 代理样例和运维脚本 |
| `docs/` | 阶段计划、RFC/ADR、威胁模型、运维和验收 |

活动规则、发布命令和筛选说明见[挑战与成就指南](docs/challenges.md)。示例活动使用固定的 2026 年 UTC 日历窗口，结束后不会自动延长。

阅读 [设计索引](docs/README.md)、[内容规范](docs/content.md)、[贡献指南](CONTRIBUTING.md)、[安全边界](SECURITY.md) 和 [部署与恢复手册](docs/operations.md)。可选模型配置与真实质量边界见 [AI 辅导指南](docs/local-coaching.md)。公开发布前的授权事项见 [LICENSES.md](LICENSES.md)。
