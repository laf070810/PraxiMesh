# PraxiMesh

通过真实编程练习学习计算知识：用结构化知识导航找到学习方向，也可以直接筛题、写代码、运行和提交。

当前交付为**已完成本地验收的自部署版本**。在线评测和 AI 辅导默认关闭，配置并通过各自门槛后才能使用。GPU 评测尚未实现；公开运营和项目许可证仍待维护者决定。产品需求见 [PRD.md](PRD.md)，完整范围与未完成项见[需求追踪](docs/requirements.md)。

## 可以做什么

- **学习与练习**：26 道题、50 个知识节点、6 条自由跳转的学习路径，以及一个四阶段工程项目。教程、知识点和题目可独立访问，并相互关联。
- **真实评测**：Python/JavaScript 函数、Python 完整程序、有状态类/API、线性模型训练、加权拟合和受控工具 Agent。公开 Run 与正式 Submit 分开，成绩由可信评测端产生。
- **私有学习记录**：代码草稿、提交历史、收藏、笔记、阅读标记、学习计划、实践证据、成就与挑战进度。支持本人导出和账号删除。
- **社区与协作**：主动送审的题解、讨论、博客、评论和绑定版本的题目反馈；可撤回的公开成果与可比榜单；组织角色、版本课程及第三方题库审核发布。
- **中英体验**：中英界面，20 个知识节点与 10 道题的英文内容；缺少译文时明确回退原文。可选 AI 提示先预览再发送，不修改正式成绩。

## 从这里开始

| 目标 | 入口 |
| --- | --- |
| 先体验导航、编辑、草稿和社区 | 下方“快速体验”，无需 Docker 或模型服务 |
| 在本机运行真实评测 | [WASI 构建与判题](docs/wasi.md) |
| 部署到自己的主机 | [发布包](docs/releases.md) → [部署、升级与恢复](docs/deployment-coordination.md) |
| 在自己的编辑器中做题 | [本地公开测试与提交工具](docs/local-practice.md) |
| 新增题目、课程或学习路径 | [内容规范](docs/content.md)、[工程项目](projects/README.md)、[组织题库](docs/rfcs/009-organizations-and-packages.md) |
| 修改代码或查看设计 | [贡献指南](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md)、[设计索引](docs/README.md) |

## 快速体验

当前验证环境为 Linux x86_64、Python 3.11；仓库的 `.python-version` 和 `uv.lock` 固定开发环境。准备 [uv](https://docs.astral.sh/uv/)，在仓库根目录运行：

```sh
uv sync --frozen
.venv/bin/python manage.py migrate
.venv/bin/python manage.py content_check
.venv/bin/python manage.py content_import --translations content/en.json
.venv/bin/python manage.py project_import content/projects.json
.venv/bin/python manage.py challenge_import content/challenges.json
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

这些初始化命令用于新的本地数据库。打开 <http://127.0.0.1:8000>，从 Learn 或 Practice 开始；注册后保存只显示一次的恢复码。默认 SQLite 数据库位于忽略文件 `db.sqlite3`。本地开发服务器仅供回环访问。

评测关闭时，题目阅读、代码编辑和私有草稿仍可使用；提交会明确提示不可用。需要真实结果时，继续配置下节的 WASI worker。

已有站点按[升级指南](docs/deployment-coordination.md)操作。`content_import` 是显式内容发布，会撤回官方包中缺失的内容 ID，不应拿示例包覆盖自有目录。示例挑战使用固定的 2026 年 UTC 时间窗口，过期不会自动延期；活动发布见[挑战与成就指南](docs/challenges.md)。

## 开启真实判题与可选辅导

[WASI 指南](docs/wasi.md)包含固定解释器构建、摘要配置、真实隔离探针和 Web/worker 启动步骤。`sandbox_check` 必须在执行环境真实通过；只有匹配策略且健康的 worker 才能接收任务。缺少配置、检查记录过期或 worker 离线时，提交保持关闭。

WASI 支持这里声明的 Python 标准库与 JavaScript 同步 JSON 契约，不提供 Node、网络、文件写入、线程或任意第三方原生扩展。各题目单独声明输入、输出、资源预算和评分规则，详见[完整程序](docs/stdio-contract.md)、[类/API](docs/object-api-contract.md)和 [Agent](docs/agent-contract.md)契约。

已完成依赖同步和 WASI 配置后，可以重放真实学习闭环：

```sh
.venv/bin/playwright install chromium
.venv/bin/python scripts/browser_acceptance.py --judge
```

可选自部署模型、发送范围和实际质量限制见 [AI 辅导指南](docs/local-coaching.md)。学习计划、规则提示和实践证据不要求配置模型。

## 自部署与验收

部署目标为 Linux x86_64，使用 PostgreSQL、固定 Web/worker 镜像和 HTTPS 代理。发布目录包含 `release.json`、`source.tar.gz`、`images.tar`；先用独立确认的清单摘要校验，再通过部署协调器执行安装或升级。

1. 按[发布指南](docs/releases.md)构建、校验和导入同源镜像。
2. 按[部署协调指南](docs/deployment-coordination.md)准备私有配置、数据库角色、网络、Caddy 和服务监管，执行 `apply`；首次导入示例内容须显式选择。
3. 按[运维手册](docs/operations.md)配置健康监测、日志、备份与恢复，并在目标主机完成验收。

当前最新固定制品为 F，已验证真实 WASI 探针、容器浏览器流程、排空恢复及 E→F 保留数据的 HTTPS 升级；摘要、证据与验证边界见 [F 验收记录](docs/acceptance-f.md)。历史验收从[设计索引](docs/README.md)进入。本地生成的镜像、日志和截图保存在忽略目录 `var/`、`test-results/`，不随 Git 克隆分发。

托管 CI/CD 工作流已提供，部署工作流默认禁用且只接受手工触发。服务端 CI、专用部署 runner、公有证书续期和目标站点持续容量仍需在对应环境验证，本地回环验收不能代替这些结果。

## 开发与检查

在独立开发环境中按改动范围运行检查：

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python manage.py content_check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pytest -q
```

普通测试自动收集静态资源；真实沙箱和 PostgreSQL 专属场景会在条件不足时跳过。跳过不表示通过。需要验证数据库锁、权限或迁移时，可使用一次性 PostgreSQL 容器：

```sh
.venv/bin/python scripts/postgres_tests.py -q
```

浏览器体验检查使用独立临时数据库和回环服务，评测默认关闭：

```sh
uv sync --frozen --group browser
.venv/bin/playwright install chromium
.venv/bin/python scripts/browser_acceptance.py
```

浏览器还需要相应系统依赖，CI 使用受支持的 Ubuntu。验收脚本创建并删除合成账号，结束时清理自有进程和数据库。真实沙箱、HTTPS、发布制品检查分别按 [WASI](docs/wasi.md)、[HTTPS 验收](docs/production-acceptance.md)和[发布指南](docs/releases.md)执行。

修改依赖后运行 `.venv/bin/pip-audit` 并按[贡献指南](CONTRIBUTING.md)更新锁文件。不要在正在运行的 Web/worker 环境中执行会增删依赖的 `uv sync`；服务使用固定环境，更新在独立环境准备。

## 代码导航

| 目录 | 职责 |
| --- | --- |
| `core/` | 知识与题目版本、账户、Web 页面、队列与资源预约 |
| `judge/` | 可信结果比较、WASI 执行器和独立的 gVisor 适配器 |
| `learning/`, `challenges/` | 私有学习记录、实践证据、成就与固定版本挑战 |
| `community/` | 题解、讨论、博客、版本题目反馈、评论与审核申诉 |
| `organizations/` | 组织角色、版本课程、第三方题库归属和发布 |
| `coaching/` | 私有目标、版本计划、诊断与可选 AI 提示 |
| `practice_api/` | 有限用途令牌、公开任务包和本人提交 API |
| `projects/`, `showcase/` | 多阶段项目、自愿公开作品与分组榜单 |
| `localization/`, `locale/` | 精确源版本译文、中英界面与原文回退 |
| `content/` | JSON Schema、示例内容、参考实现与维护测试 |
| `templates/`, `static/` | 页面、样式和渐进交互 |
| `scripts/`, `deploy/`, `.github/` | 构建验收、发布协调、服务示例与 CI/CD |
| `tests/`, `docs/` | 自动检查、RFC/ADR、维护指南与验收记录 |

## 贡献、安全与许可

开发前阅读[贡献指南](CONTRIBUTING.md)；开发代理还应遵循 [AGENTS.md](AGENTS.md)，随实现维护文档并提交可审阅的增量。题目内容和版本规则见[内容规范](docs/content.md)，反馈与审核见[社区指南](docs/community.md)。

安全问题按 [SECURITY.md](SECURITY.md)处理，执行与隐私边界见[威胁模型](docs/security.md)。代码、原创内容和投稿的公开许可尚未确定，详见 [LICENSES.md](LICENSES.md)；依赖与运行时仍遵循各自许可证。
