# F：版本题目反馈与自部署升级验收

日期：2026-10-24。补充 PRD §7.2/R-13 的题目反馈、错误报告与修订建议，设计见 [RFC-014](rfcs/014-versioned-problem-feedback.md)，使用见[社区指南](community.md)。

题目反馈绑定明确版本，支持草稿、主动送审、按类型审核、公开讨论、编辑后重新审核、撤回、账号导出和删除。审核通过只表示公开反馈，不表示题目已经修正。迁移只新增类型和版本约束；原目录与执行策略保持不变。

## 源码验证

首轮定向检查为 127 passed、3 failed。失败分别来自新增测试误用内容字段 `version`（实际为 `revision`，中英文各一项），以及读者与作者共用测试客户端；已修正测试。随后真实 PostgreSQL 定向复验 **153 passed**，包含实际旧迁移到新迁移、四种原帖子保留、数据库拒绝缺少版本的反馈、历史版本、权限、导出删除、中英界面及验收控制。记录为 `test-results/f-community-unit.{log,xml}` 和 `f-community-postgres.{log,xml}`。一次性容器和数据库卷清理通过。

部署控制另有 **81 passed、1 skipped**，跳过项需要实际 PostgreSQL 权限环境；在独立 PostgreSQL 中重跑该组为 **82 passed**、无跳过，见 `f-deployment-controls-final.{log,xml}` 和 `f-deployment-controls-postgres.{log,xml}`。最初一次命令误列不存在的测试文件，未执行任何测试，保留在 `f-deployment-controls.log`。153 项与 82 项中包含重叠控制检查，不将两者相加为独立用例数。

Ruff 检查和 331 个 Python 文件格式检查通过，`makemigrations --check --dry-run` 无变化。`f-boundary.json` 确认原 26 个迁移文件、31 个题库/判题文件、执行策略、原 HEAD、暂存区及 PRD 摘要保持不变；唯一新迁移为 `community.0003_problem_feedback`。

## 固定发布包

制品目录为 `var/releases/2026-10-24-f-candidate-1`，包含 464 个冻结源码文件。

| 身份 | SHA-256 |
| --- | --- |
| 发布清单 | `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` |
| 源码 | `1aa1030de3eb5214aa85d3f7b6b1f17815a1d537f68327a9e2f206215bb85090` |
| Web 镜像 | `b4a9c4918aac2fb69455ba12641d676fff7ad288e41b26afc34cf1ee6fecfa28` |
| worker 镜像 | `6cab97659661bba423468e802f2c9518b18890b698ddd8c3efdbbfcb8f1a1f39` |
| 执行策略（与 E 相同） | `f7a955218ed9b662a2f878949f11014ed77bc84faefae9e6dfac6c6b9db5608a` |

`test-results/f-release-final/` 保存首次安装回执、启动/浏览器日志、截图及源码比对。129 个真实 WASI 启动探针、基础浏览器、三组语言/项目/CLI 流程及七组扩展流程全部通过；扩展包含专业评测、博客、题目反馈、组织、辅导、挑战和类/API。验证了非 root、只读根文件系统、私有 worker 网络、无 Docker socket 挂载，实际排空超时/拒绝新提交/完成原任务/恢复、错误运行时拒绝，以及容器/网络/数据库卷清理。此首次安装浏览器连接回环 HTTP；HTTPS 由下述实际升级验证。

新增反馈浏览器证明明确版本、未送审草稿对审核者不可见、分类审核、公开列表、纯文本转义、英文 390 像素移动页无横向溢出、编辑后重新审核、撤回、仅导出本人反馈及删除账号。所有合成账号清理通过。AI 仍默认关闭；本轮不重跑模型服务或 GPU。

## E→F 实际 HTTPS 升级

2026-10-23 22:22:23 UTC 完成，回执为 `test-results/deployment-f-upgrade/deployment-acceptance.json`。旧 E 清单为 `83514049f13a3249259a61815c7da44129c92853865be8ff791dd98815b0939c`；新清单为上表 F。两代使用各自冻结镜像，运行真实启动探针和判题检查。

在 E 中建立真实正式评测、草稿、笔记、版本题解、访问令牌、幂等提交、公开成果、组织课程与私有学习计划，随后通过部署协调器排空、备份、恢复到独立候选库并迁移。确认仅增加反馈迁移，不增加表，不自动发布或改写题库；原行摘要、序列、私有导出、正式资源预约和课程保持一致，Web/worker 社区权限不扩大。旧令牌重复提交返回原任务且不创建新任务。

升级后的 HTTPS 核心浏览器与题目反馈浏览器均通过；包括真实 Run/错误/Accepted、公开审核、英文移动页、撤回和账号删除。最终 `cleanup.passed=true`，无遗留自有资源，命名数据库卷已删除，本地进程终止。此次没有重跑失败迁移或中断恢复矩阵，也没有重跑 E 的容量测试；这些历史证据仍按原记录引用。

两次验收期间源码逐文件一致；归档分别包含 `source-audit.json`。最终补充的交付文档与冻结包差异另记于 `f-source-audit.json`，运行代码、迁移、内容和测试仍与验收制品一致。

GPU、外部部署/托管 CI、公开运营及项目许可证继续按[需求追踪](requirements.md)保留，本次不宣称全部 PRD 已完成。
