# D：完整筛选、挑战与成就的本地验收

日期：2026-10-24。本轮补齐 PRD §3.3 的完整题型/环境/库/资源筛选，以及 §7.2 的自愿挑战活动与私有成就。操作说明见[挑战与成就指南](challenges.md)，设计见 [ADR-012](adr/012-challenges-achievements-and-discovery.md)。自部署的这些本地增量已验证；真实 GPU、gVisor、托管 CI、公网与许可证仍按[完整需求追踪](requirements.md)保留后续事项，全部 PRD 目标未宣告完成。

## 冻结发布包

- 目录：`var/releases/2026-10-24-d-candidate-2`，含源码档案、Web/worker 镜像档案和清单。
- manifest SHA-256：`cffe00e864a47ef335ae23245285a0d3ada31987b8ec486b40b882c01ad73675`。
- source ID：`c9a392db0f3d615e5623ee2d6d77d95a7dcf346e0c1307a2fd20edba68512e5d`，共 447 个冻结文件。
- Web image：`sha256:918f3393086fe7ea27b662a8215f36279330ed152810ace2be004f1d24e9f409`。
- worker image：`sha256:8ca7917608f5df1cad60b08441d422c9a1aacd95d824bfe32655d50d8de54f23`。
- WASI bundle：`56d8e49398f508f793c71f5c350efbc27d27d219502aa8f9a7b8ca06b24c1c0b`；策略：`b3914277f73f6a8fc4c6bda98501d155844e9a58ce2df812d19d37cedce50877`。

最终源码先冻结再构建，并逐文件核对与工作区一致。D1 通过固定镜像首次安装；第一次 C→D 演练在访问隔离断言失败：游客会被引导登录，脚本却期待 404。D2 改用另一个已登录账号验证 404，另更新命令帮助和历史文档状态。实际比较确认 D1/D2 的 Web、worker RootFS 层和运行配置（除发布标签）完全相同，因此首次安装证据复用 D1，实际升级在 D2 上重新执行。未将 D1 初装回执改写为 D2 回执；映射见 `test-results/d2-runtime-comparison.json`。随后仅补写验收和交付文档，不将后写文档算入旧 source ID。制品导入与部署入口见[发布指南](releases.md)和[部署协调指南](deployment-coordination.md)。示例挑战有固定的 2026 年 UTC 窗口，不会在过期后自动续期。

## 功能、权限与真实执行

| 检查 | 实际结果 |
| --- | --- |
| 新功能与既有账户/学习/项目/发布定向 PostgreSQL 回归 | 212 passed；涵盖不可变导入、完整回滚、UTC 窗口、固定版本、本人证据、退出重入、导出/删除、Web/worker 权限与并发加入 |
| 普通全回归 | 原始 1170 passed、16 failed、77 skipped（共 1263 项）；16 项均因执行环境禁止创建本地 socket 失败，日志保留 |
| 本地 socket 环境重跑 | 相关模型传输与代理恢复两组 100 passed；逐 testcase 核对覆盖并通过全部原始 16 个失败。合并证据覆盖 1186 个可执行检查，不声称原始全回归一次全绿 |
| 中英与呈现定向回归 | 55 passed；包含完整筛选与活动文案 |
| 升级前置契约 | 51 passed、1 PostgreSQL-only skipped；实际 schema 升级另行执行 |
| 固定镜像首次安装（D1；D2 镜像文件层/运行配置相同） | 123 个真实启动探针，基础/英文/项目/本地工具浏览器，以及专业评测、博客、组织、AI 关闭状态和挑战五组扩展流程全部通过 |
| 新挑战实际 Chromium/WASI | 两账号；键盘加入、自由选择任务、Run 不计完成、两个正式通过更新私有进度和成就、跨账号拒绝、导出、退出重入和账号删除全部通过 |
| C4→D 实际升级 | 候选数据库备份/恢复、新 migration、旧数据与资源记录保留、独立发布新活动、真实活动通过及账号删除通过 |
| Python 依赖 | 54 个本机包，未报告已知漏洞；不包含完整 OS 或模型二进制审计 |

首次真实 PostgreSQL 尝试发现参与唯一约束与表同名，导致建表失败。在发布前更正初始 migration 的约束名称后，从新的临时数据库重验通过。失败记录保留在 `test-results/d-feature-attempt-1/`，不是成功回执。

77 个普通测试跳过项包括 54 个实际沙箱场景和 23 个 PostgreSQL 专属检查。新功能的 PG 与真实运行已另行验证；判题、vendor、资源政策和依赖的 36 个文件与 C4 完全一致，24 个既有迁移文件也保持一致。C 的完整 PG/WASI 原始回归、修复重放及失败迁移证据见 [C 验收](acceptance-c.md)。本轮没有重跑完整 PG/WASI 全矩阵、旧五代中断矩阵或新的故障迁移制品。

## C→D 数据保留

旧版使用已验收的 C4 清单 `8825b719e7b868ab446955489fb0190c6cf3f30428621c6d46a879fcfad2f9b5`。演练在旧版通过 HTTPS 创建私有草稿、真实正式成绩、令牌与幂等请求、收藏/笔记/阅读、社区草稿、自愿成果、组织角色及固定版本课程、本人学习计划。

升级迁移不自动发布内容。升级前后核对完整提交行（包括资源预约字段）、私有数据、课程/计划、目录、项目版本和翻译；同时保留会话、英文偏好与令牌重放语义。三张活动定义表对 Web 只读，参与表可写，worker 无权读取四张新表；实际拒绝 SQL 和精确表/序列权限均检查。

核对后，owner 独立发布一份固定任务和时间窗口的活动。用户自愿加入，旧通过不计入，新 WASI 正式通过更新活动与成就；退出重入恢复零进度。删除账号清除参与、学习计划、课程加入与本人的成员记录，撤销令牌/成果访问，同时保留公开活动定义与组织课程的既有删除边界。完成时间为 `2026-10-23T20:00:28.857679+00:00`。没有手改 verdict、资源账本、迁移历史或部署 journal。

## HTTPS、界面与容量范围

独立冻结源码上的真实 Caddy/Gunicorn/PostgreSQL/WASI 验收于 `2026-10-23T19:23:35.724771+00:00` 完成：TLS/主机名/错误证书、代理边界、最小权限、安全 cookie、全部扩展浏览器、worker 停机降级及清理通过。

六账号并发正式提交全部通过，最慢 36.292 秒；判题期间八客户端 120 次浏览零错误，p95 0.2039 秒。它是共享主机的小样本本地回归，不是公网 SLA 或目标服务器容量保证。

该轮 HTTPS 源码在最终导航/语言提示调整之前冻结。两者之间的应用 Python 变化仅为格式，最终界面、英文筛选与 390 像素手机布局随后由固定镜像浏览器重新验证并人工查看截图；差异核对在 `test-results/d-production-release-delta.json`。所有本轮临时服务、容器、网络、数据卷、密钥和测试账号均已按各回执清理。

主要原始证据：`test-results/d-postgres-feature-corrected.{log,xml}`、`d-application.{log,xml}`、`d-socket-controls.{log,xml}`、`d-presentation.{log,xml}`、`d-evidence-audit.json`、`d2-runtime-comparison.json`、`deployment-d-upgrade-attempt-1/`、`d-production-final/`、`d-release-final/`、`deployment-d-upgrade/`。这些包含合成验收数据，保存在忽略目录中；它们与部署指南共同界定验证范围。
