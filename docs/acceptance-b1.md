# B1 学习与社区验收

日期：2026-10-23。对应 [RFC-006](rfcs/006-complete-product.md) 的第一批与 [ADR-009](adr/009-learning-community.md)。阶段 A 历史证据见 [acceptance.md](acceptance.md)。本批完成不等于全部 PRD 完成；[需求追踪](requirements.md)继续覆盖 B2/C 和外部发布条件。

## 交付

题库共 18 题、38 个知识节点、8 篇教程、3 条路径。原冻结版本的 8 题与 20 个节点逐字段不变。全部 18 题的参考解实际通过 WASI 的 125 个用例；36 个针对性错解与每题一个恒空变体被拒绝。新增路径分别覆盖序列/图算法、统计/数值与建模基础。

用户可收藏题目、保存版本笔记、自述已读教程，并按当前正式结果查看领域/概念证据和解释清楚的复习建议。Run、平台错误和旧版结果不计当前完成。学习数据私有，纳入导出/删除。

社区提供讨论、提问、版本题解、评论、主动送审、明确权限审核、私有举报/申诉和撤回/删除。已公开正文编辑后重新待审。父讨论不可见时普通访问不能通过评论绕过；审核者可单独处理已送审评论而不读取其私有父文。只保留不含正文的最小审核事件。

## 实测证据

- 最终普通回归：412 passed、18 环境相关 skipped，`test-results/application-b1-final.xml`。Ruff、格式、Django 配置与迁移一致性检查通过。
- PostgreSQL/WASI 全回归：405 passed、1 SQLite 专属 skipped，`test-results/postgres-b1.xml`。真实隔离与全部内容执行未跳过。
- 最后评论入口及审核边界修复、升级选项与授权专项：PostgreSQL 118 passed，`test-results/postgres-b1-integration.xml`。
- 本地 Chromium：学习区及社区多账号全流程通过；`test-results/b1-local-community-browser.json`。通过真实表单执行发布、重审、评论、举报处理、隐藏、申诉、重新审核、导出、撤回、删除。三个合成账号实际删除。草稿导出按浏览器 CRLF 规范化比较，保留其余全部字符。
- 冻结镜像首次安装：`test-results/release-b1-acceptance.json`，通过。验证只读非 root Web/worker、无宿主目录或 Docker socket、内部 worker 网络、21 个启动探针、真实 Run/WA/AC、学习浏览器流程、排空/恢复及错误制品拒绝。首次导入扩充目录。
- 真实 schema 升级：`test-results/deployment-b1-upgrade/deployment-acceptance.json`，通过。先安装冻结阶段 A，创建真实用户、草稿和正式 AC；再用真实备份/恢复和迁移升级到 B1。旧数据行摘要及身份保持，旧题库保留为 8 题，不自动覆盖维护者目录。新增 `learning.0001_initial` 与 `community.0001_initial`，8 张新表出现；实际 Web CRUD 成功、worker CRUD 被数据库拒绝。
- 升级后通过回环 HTTPS 再次执行真实判题、学习数据读写/导出，以及完整多账号社区 Chromium 流程，见同目录 `community-browser.json`。原用户新提交获真实 AC。
- 两次镜像演练的私有容器、网络、卷和进程均清理；升级回执 `remaining_owned_resources={}`。未修改系统证书信任或部署公网实例。

## 冻结制品与范围

本批制品：`var/releases/2026-10-23-b1-candidate-1/`。

| 身份 | 值 |
| --- | --- |
| release.json SHA-256 | `9fc285118c038e0b403fdbfa56cd14646e49d50ec76950260df6d16263f8b4e7` |
| source ID | `2da55432604033e456d0949251730a33dfb63914cf9dc8a90b554bd76729c1e1` |
| Web image | `sha256:c57d86410f1631717767dd2a05cb55e7a361f776aea3a43725b378535ae87555` |
| worker image | `sha256:5fbf328c73dfdd702067286c41e3ea76a7e403ab16f352c0556f6b0664593ebb` |
| WASI bundle SHA-256 | `35a76bda43a52c1db3ba0e4627922264d26e4486dc17722d49c7b374fbba216b` |

升级前版本为阶段 A candidate-9，manifest SHA-256 `f762d888e51eafb442c452b48eb0b39bbe3fde834c29fc8296efb6ab4af29628`。本次证明上述两个新增应用迁移可保留数据升级；不保证任意未来迁移，也没有在 B1 重跑旧版五代中断/失败迁移矩阵。既有阶段 A 的这些历史证据仍按其原制品记录。

专业题型、第二语言/完整程序、中英体验、公开作品榜单、项目/本地工具、组织生态和 AI 辅导在后续批次实施。公开运营、授权政策、公有域名/证书和托管 CI 尚未验收。GPU 设备枚举和 QuickJS 原型实验只用于后续设计，不计入本批产品能力。
