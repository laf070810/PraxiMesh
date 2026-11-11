# F 升级准备与当前 CPU 发布包验收

日期：2026-11-11 至 12 日。对应 PRD R-09、AC-07。操作入口统一在[生产部署指南](deployment/production.md#f-current-upgrade)。

F→当前版本的升级检查已接入，当前 CPU 包也已通过真实 WASI 与浏览器检查。旧 F 镜像尚未导入或启动，完整跨版本应用升级仍未验收。

## 组件与数据库

提交 `fc592650201843ad6a87141f9f57cfc298c7a5fb` 新增独立的 post-F 分支，核对冻结 F 的十七个历史迁移与当前三个新增迁移，拒绝改写历史、未知组合、相同镜像和 GPU 包。旧升级分支保留。

相关组件为 **94 passed / 2 skipped**；随后在一次性 PostgreSQL 中完成两项被跳过的检查及四项历史 schema 检查，共 **6 passed / 0 skipped**。它们覆盖数据快照、GPU 空表、列权限与旧写入兼容，未运行旧 F 应用。实际 F/当前清单的只读预检从拒绝变为接受。

## 当前包的真实验收

最终通过的包在 `var/releases/2026-11-11-cpu-upgrade-target-2/`：

| 身份 | 值 |
| --- | --- |
| 源提交 | `fc592650201843ad6a87141f9f57cfc298c7a5fb`；974 个冻结文件逐字节匹配 |
| manifest SHA-256 | `9145e0844e562c3cb56e542faf9c15e40bb5001d73b8a0307334ad9b8623e211` |
| source_id | `701eabe544fe5a1fe6d88716f38bff20e592f6877a3ed4b1f7ad7c0c97910d8e` |
| WASI bundle | `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d` |

完整镜像与源码归档先校验，再在私有网络、独立 PostgreSQL 和回环 HTTP 站点运行。129 项镜像内真实启动探针、Run/错解/正确提交/进度/删除、三组基础及七组扩展浏览器旅程、准入超时关闭、排空/恢复和错误运行时拒绝均通过。临时容器、网络与数据库卷清理通过，独立 Docker 客户端配置已删除。本轮没有使用 AI、HTTPS 或 GPU；CPU/HTTPS 完整发布恢复矩阵见 [G59](acceptance-g59.md)。

此次使用提交 `10242e5` 的外部浏览器验收脚本；归档仍为原始 `fc59265`，未就地改写。脚本摘要保存在 `polling-harness-sources.json`。

## 保留的失败与调整

- 较早目标包的首次运行因验收调用者缺少匹配的 WASI 摘要而失败。补齐调用环境后在新目录重跑通过，原失败保留。
- 本目标包首次运行在专业实践旅程的 `page.reload()` 处出现 Playwright `Error`。页面本身已在任务结束后自动刷新，验收脚本改为等待真实终态面板，沿用原三十秒预算及原有结果断言；随后完整发布包验收通过。
- 使用实际页面脚本的六次独立导航诊断均完成，**未复现原导航错误**。因此只确认额外导航已移除及全流程复验通过，不据此认定首次错误的确切原因。

最初与最终只读服务观察中，现有 Web、worker 和 tutor 的容器 ID/镜像一致，均在运行；worker 启动时间从 11 月 10 日变为 11 月 11 日，最终观察到重启计数为 9。因此最后的“启动时间全部不变”审计未通过；本轮没有对这些既有服务执行启动、停止或重建，也未据当前观察推断重启原因。

随后只读检查原 worker 日志：上一轮 129 项探针通过后 86400.8 秒，出现凭证过期/不匹配退出；原容器自动重启并再次通过全部 129 项探针，当前健康。这与 [Compose 的 24 小时凭证续检机制](deployment/compose.md)一致。原始审计差异保留，后续证据在 `test-results/hosted-ci-2026-11-11/worker-renewal-confirmation.json`，未对服务执行任何变更。

## 剩余门槛与证据

旧 F 归档 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` 的完整内容已重新只读核验。其 Web 镜像缓存缺失；此前导入被自动审批拒绝，见 [G44](acceptance-g44.md)。恢复缓存及执行隔离整站升级的许可仍待维护者回复。当前包通过、历史 schema 升级或预检接受均不替代旧应用整站升级。

原始结果在忽略目录 `test-results/upgrade-preparation-2026-11-11/`：`components-2.xml`、`postgres.xml`、两个构建结果、`current-release-result-*.json`、`navigation-reproduction.json`、`final-resource-audit.json` 与 `services-final-observation.json`。托管检查的后续失败及本地复现另见[托管记录](acceptance-hosted-ci.md#later-runs)。真实 GPU 全路径与其余产品门槛继续按[需求追踪](requirements.md)处理。
