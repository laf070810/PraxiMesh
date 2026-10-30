# ADR-016：部署 runner 的状态入口与权限边界

状态：接受，2026-10-30。依据：[RFC-017](../rfcs/017-hosted-deployment-runner.md)。目标 runner 尚未注册或验收。

部署工作流默认执行 `status`，操作者确认固定路径、配置和部署身份后再明确选择 `apply` 或 `recover`。状态操作只查询协调器记录，不代替 HTTP、worker 心跳、真实评测或恢复验收。部署激活继续使用已审查控制器、预置不可变发布包和明确摘要。

带站点权限的 runner 只接收受信部署工作流。公开应用仓库继续使用托管 runner 做 PR、构建和 WASI 验收；推荐用独立私有运维仓库承载长期部署 runner，或使用实际限制到选定工作流的组织级 runner。自定义标签和单个 job 的 environment 不能约束其他工作流。没有可用保护机制时保留主机手工协调器入口。

部署权限与 GPU 任务执行权限分开。GPU 后端仍未实现；硬件、驱动或 runner 注册不构成真实 GPU 评测验收。实际接入步骤统一见 [部署指南](../deployment/README.md)。
