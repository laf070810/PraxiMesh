# 当前源码的 CPU 与四镜像发布包

日期：2026-11-13。对应 PRD R-09/15/16。两份新包均从干净、已推送的 `0b9a69c6c8d7a700c62a3c9e4e579a129616de14` 构建，包含[镜像源码权限修复](acceptance-image-staging.md)。原 G42 四镜像包保留原身份；它缺少后续代际配置声明，旧契约不能用于当前 worker。

## 制品与用途

| 格式 | 用途 | 不可变目录 | manifest SHA-256 |
| --- | --- | --- | --- |
| format 1 | 当前公共 CPU 发布协调器及旧 F 升级验收目标 | `var/releases/2026-11-13-cpu-upgrade-target-4/` | `767551ab285abe431a5e0ca692d0a34631a122227dd80510ca2a97e24e4e0741` |
| format 2 | 保存配套 GPU 镜像，供后续硬件和完整生命周期验收 | `var/releases/2026-11-13-four-image-candidate-1/` | `3d0aad15acfb33e71a56ffe1af7b9fc5a4b7e5f6b4fb360f3e5c2e515077bdd0` |

两份包的源码 ID 均为 `31985d5dc9a0782d529c12a8ebe59ef2e1a7a5983e38a1a4693a032b74de8524`，981 个文件与该提交的原始 Git 对象逐字节匹配。源码归档、Web/CPU worker 镜像 ID、平台和评测配置完全相同；format 2 另保存 GPU worker 与任务镜像，以及 `generation-pair-v1` 声明和归档内的有序源码范围。

| 角色 | 不可变镜像 ID |
| --- | --- |
| Web | `sha256:cf434d232cf9e76e2b2ad1235161e9600aaba536622db575d0539aa6d2ce2456` |
| CPU worker | `sha256:92f624fec8f0639f8328d81707da15cf7314a2c0c3ea7a114dc89103b0c9e820` |
| GPU worker | `sha256:2131d60d4cce24da58b9df1f40d8a7007559b739ff180fedad9fe1096a4b9ccb` |
| GPU task | `sha256:3d77b9954d3abcd8a6e7e8303d85ae6d615f4af5e6125a00691952b9468d7d30` |

WASI bundle 为 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`，CPU 策略为 `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9`。构建使用 `umask 077`，分别完整核验源码、实际镜像层、角色及契约；没有覆盖旧发布目录。

## 实际导入与应用检查

四镜像包构建结束后，临时 GPU worker 标签及其缓存镜像被构建器正常清理。通过标准发布入口重新核验并导入新归档，再对包内的确切 worker 执行 **10 项真实 Compose/PostgreSQL 预检**，全部通过。范围包括最小授权、重建、默认关闭、配置/权限/源码拒绝、恢复原源码和旧控制配置拒绝；GPU 查询、映射和执行均为 false。

随后在独立 PostgreSQL、私有 Docker 网络和回环 HTTP 站点运行同包的 Web/CPU worker，完成：

- **129 项新实例启动探针**，以及非 root、只读根、Web 无 WASI 运行时、无宿主目录或 Docker socket 挂载检查。
- 实际 Run、错误与正确 Submit、进度/历史和账号删除；三组基础及七组扩展浏览器旅程。
- 准入超时保持关闭、真实任务排空与恢复、错误运行时在 worker 启动前拒绝。
- 自有容器、网络和数据库卷清理；私有 Docker 客户端配置移除。

CPU format 1 包另行完成完整校验与标准导入。其应用证据引用上述同一对不可变镜像的一次实际运行，依据是镜像 ID、源码归档和评测配置逐项相同；没有把它计为第二轮应用测试。本地应用检查的范围为 CPU 与回环 HTTP，AI 和 GPU 未参与。

实现提交 `fa58c7a` 和本批制品源提交 `0b9a69c` 的托管应用及完整 WASI 工作流也已通过，包含独立构建制品的 HTTPS、发布及迁移失败/中断恢复检查，见[托管记录](acceptance-hosted-ci.md#later-runs)。此证据绑定代码提交；长期目标站点仍须单独验收。

## 原资源与剩余门槛

最终只读审计确认，worker 预检的 11 个容器、两个网络和数据库卷已移除，CPU 应用的清理及卷移除检查通过，构建的两个临时 GPU 标签不存在。导入的四个不可变镜像按发布目的保留。原 Web、CPU worker、tutor 的容器 ID、镜像、启动时间、重启计数及健康状态与本轮开始一致。

旧 F 与新 CPU 目标均通过完整归档读取，升级组合预检返回 `post-f`。旧 F 镜像导入许可仍待回复；未启动旧应用或执行跨版本升级。GPU 内部整站编排已经串接，公共 `apply/recover` 仍关闭，完整设备启动、队列、性能、升级及故障恢复矩阵仍待授权后的真实验收。

操作统一在[发布指南](deployment/releases.md)，旧 F 当前目标与授权边界见[升级准备](acceptance-upgrade-preparation.md#current-target)。本轮制品和无设备应用通过不代表完整 PRD 已完成。

## 原始记录

记录集中在忽略目录 `test-results/current-four-image-release-2026-11-13/`：`preflight.json`、`browser-preflight.json`、`release-build-result.json`、`pre-import-inventory.json`、`released-worker-process.json`、`cpu-application/release-acceptance.json`、`cpu-release-build-result.json`、`cpu-release-import.json`、`latest-upgrade-preflight.json` 和 `final-resource-audit.json`。

worker 原报告为 `test-results/px-container-worker-d210be9636c3/result.json`。构建和应用日志、浏览器截图均保留于私有忽略目录，未加入发布源码或 Git。
