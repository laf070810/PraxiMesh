# ADR-096：PostgreSQL Compose 使用独立应用入口

日期：2026-11-16。状态：分支实施中；完整站点仍未交付。关联 [RFC-042](../rfcs/042-complete-gpu-compose-site.md)。

保留 `core.selfhost` 的 SQLite 限制，新增独立 PostgreSQL Web/CPU worker 入口。入口只接受显式安装标识、对应数据库与最小角色、准备好的密钥和冻结 WASI 摘要；不创建数据库、不迁移、不发布内容，也不产生 GPU 就绪。准备与维护程序另行负责原资源身份、最小权限、冻结应用迁移和版本恢复。

本地 AI 配置及只读 CPU 健康检查复用既有实现。普通 Web 镜像准备私有 `/ai` 挂载点，使可选 CPU tutor 可接入 PostgreSQL 方案；只有 Web 读取该卷，CPU worker 不保留模型凭据。GPU worker 仍使用原专用入口及私有配置，不接收 Web 的数据库/AI 环境。

应用入口的配置检查与镜像检查分别验证；它们不能替代整个 PostgreSQL/GPU Compose 方案的真实启动、数据保留和恢复。新应用模块须纳入 worker 镜像源码契约，后续只使用匹配的新冻结包验证。

分支验证：`tests/test_selfhost.py` 与 `tests/test_postgres_selfhost.py` 共 33 项通过，包括 SQLite 兼容、跨角色/数据库拒绝、无初始化副作用和 AI 凭据隔离；实际镜像与 PostgreSQL 运行仍待执行。
