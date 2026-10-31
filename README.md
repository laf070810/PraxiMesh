# PraxiMesh

通过真实编程练习学习计算知识：用结构化知识导航找到学习方向，也可以直接筛题、写代码、运行和提交。

当前交付为**已完成本地验收的自部署版本**。默认 Docker Compose 包含真实 CPU/WASI 评测；可选覆盖文件提供本地 CPU AI 辅导。GPU 正式评测仍在开发：宿主 worker、持久成绩、健康准入与独立队列路由已接通，默认关闭；完整硬件验收、容器化部署及正式题型发布尚未完成。公开运营和项目许可证仍待维护者决定。产品需求见 [PRD.md](PRD.md)，完整范围与未完成项见[需求追踪](docs/requirements.md)。

## 可以做什么

- **学习与练习**：26 道题、50 个知识节点、6 条自由跳转的学习路径，以及一个四阶段工程项目。教程、知识点和题目可独立访问，并相互关联。
- **真实评测**：Python/JavaScript 函数、Python 完整程序、有状态类/API、线性模型训练、加权拟合和受控工具 Agent。公开 Run 与正式 Submit 分开，成绩由可信评测端产生。
- **私有学习记录**：代码草稿、提交历史、收藏、笔记、阅读标记、学习计划、实践证据、成就与挑战进度。支持本人导出和账号删除。
- **浏览器编辑器**：Python/JavaScript/CUDA 高亮、补全、本地检查、格式化、查找替换和常用快捷键；CUDA 检查仅提示语法结构，真实评测仍需 GPU 环境。[使用说明](docs/editor.md)。
- **内容扩展**：沿用知识 ID、版本和审核发布流程；新增 [CUDA 候选内容与契约](docs/cuda-contract.md)，可在独立验收环境准备向量加法和矩阵乘法题。候选未进入默认题库，真实 GPU 验收仍待完成。
- **社区与协作**：主动送审的题解、讨论、博客、评论和绑定版本的题目反馈；可撤回的[公开成果与分组榜单](docs/sharing.md)；组织角色、版本课程及第三方题库审核发布。GPU 计时榜规则和页面已接入，实际 GPU 成绩仍需完成硬件验收。
- **中英体验**：中英界面，20 个知识节点与 10 道题的英文内容；缺少译文时明确回退原文。可选 AI 提示先预览再发送，不修改正式成绩。

## 部署与运行

**统一入口：[部署指南：方案选择、启动与维护](docs/deployment/README.md)。** 指南比较 Compose 单机自部署、PostgreSQL/HTTPS 发布协调器和源码开发，说明各自的适用场景、数据位置、AI 选择及升级方式。

想直接做题，在 Linux x86_64 主机准备 Docker Engine 和 Docker Compose，于仓库根目录执行：

```sh
docker compose up --build -d
```

需要同时启动本地 CPU AI：

```sh
docker compose -f compose.yaml -f compose.ai.yaml up --build -d
```

打开 `http://服务器IP:8000`。默认监听 `0.0.0.0:8000`；首次自动构建解释器、初始化新数据库并启动真实 worker，隔离检查完成后开放评测。使用 AI 时，后续命令也带这两个 `-f` 参数。端口、服务状态、数据保留、更新和恢复都在[部署指南](docs/deployment/README.md)查阅。

| 其他任务 | 入口 |
| --- | --- |
| 用 Python 修改代码或内容 | [源码开发环境](docs/deployment/README.md#development) |
| 接入 GitHub CI 或检查 GPU 节点 | [CI 与部署 runner](docs/deployment/ci.md)、[GPU 检查入口表](docs/deployment/gpu.md#validation-options)；按是否接入设备选择，真实设备映射需要明确授权 |
| 在自己的编辑器中做题 | [本地公开测试与提交工具](docs/local-practice.md) |
| 新增题目、课程或学习路径 | [内容规范](docs/content.md)、[工程项目](projects/README.md)、[组织题库](docs/rfcs/009-organizations-and-packages.md) |
| 贡献代码或查看设计 | [贡献指南](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md)、[设计索引](docs/README.md) |

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

浏览器还需要相应系统依赖，CI 使用受支持的 Ubuntu。验收脚本创建并删除合成账号，结束时清理自有进程和数据库。真实沙箱、HTTPS、发布制品检查分别按 [WASI](docs/deployment/wasi.md)、[HTTPS 验收](docs/deployment/validation.md)和[发布指南](docs/deployment/releases.md)执行。

修改依赖后运行 `.venv/bin/pip-audit` 并按[贡献指南](CONTRIBUTING.md)更新锁文件。不要在正在运行的 Web/worker 环境中执行会增删依赖的 `uv sync`；服务使用固定环境，更新在独立环境准备。

## 代码导航

| 目录 | 职责 |
| --- | --- |
| `core/` | 知识与题目版本、账户、Web 页面、队列与资源预约 |
| `judge/` | 可信结果比较、WASI 执行器、gVisor 适配器及开发中的 GPU 组件 |
| `learning/`, `challenges/` | 私有学习记录、实践证据、成就与固定版本挑战 |
| `community/` | 题解、讨论、博客、版本题目反馈、评论与审核申诉 |
| `organizations/` | 组织角色、版本课程、第三方题库归属和发布 |
| `coaching/` | 私有目标、版本计划、诊断与可选 AI 提示 |
| `practice_api/` | 有限用途令牌、公开任务包和本人提交 API |
| `projects/`, `showcase/` | 多阶段项目、自愿公开作品与分组榜单 |
| `localization/`, `locale/` | 精确源版本译文、中英界面与原文回退 |
| `content/` | JSON Schema、示例内容、参考实现与维护测试 |
| `templates/`, `static/` | 页面、样式和渐进交互 |
| `frontend/` | 编辑器源码；构建资源随站点交付，部署无需 Node |
| `scripts/`, `deploy/`, `.github/` | 构建验收、发布协调、服务示例与 CI/CD |
| `tests/`, `docs/` | 自动检查、RFC/ADR、维护指南与验收记录 |

## 贡献、安全与许可

开发前阅读[贡献指南](CONTRIBUTING.md)；开发代理还应遵循 [AGENTS.md](AGENTS.md)，随实现维护文档并提交可审阅的增量。题目内容和版本规则见[内容规范](docs/content.md)，反馈与审核见[社区指南](docs/community.md)。

安全问题按 [SECURITY.md](SECURITY.md)处理，执行与隐私边界见[威胁模型](docs/security.md)。代码、原创内容和投稿的公开许可尚未确定，详见 [LICENSES.md](LICENSES.md)；依赖与运行时仍遵循各自许可证。
