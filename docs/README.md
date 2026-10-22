# PraxiMesh 设计与交付记录

需求基线：[PRD v0.2](../PRD.md)。技术方案可变，产品验收不随实现缩减。

- [阶段计划与安全发布门槛](plan.md)
- [RFC-001：阶段 A 的架构、内容和可信评测](rfcs/001-phase-a.md) — 接受，2026-10-22
- [ADR-001：应用和数据边界](adr/001-application.md) — 接受
- [ADR-002：评测隔离与结果信任边界](adr/002-evaluation.md) — 接受，运行环境待验证
- [ADR-003：身份、部署与测试](adr/003-operations.md) — 接受
- [RFC-002：基础 Python 的 WASI 执行](rfcs/002-wasi-python.md) — 接受
- [ADR-004：WASI 的能力和资源边界](adr/004-wasi.md) — 接受，本地真实验证
- [RFC-003：HTTPS 部署验证与发布证据](rfcs/003-production-acceptance.md)
- [ADR-005：代理信任与真实 HTTPS 验收](adr/005-production-acceptance.md) — 接受，隔离部署实测
- [RFC-004：一致版本的发布制品](rfcs/004-release-artifacts.md) — 制品已验证
- [ADR-006：Web/worker 不可变制品](adr/006-release-artifacts.md) — 接受，实际镜像闭环验证
- [RFC-005：任务排空与版本协调](rfcs/005-deployment-coordination.md) — 协调器已实现，本地四代部署与中断恢复通过
- [ADR-007：暂停入口与策略绑定](adr/007-admission-coordination.md) — 接受
- [ADR-008：维护窗口发布与保留数据的恢复](adr/008-deployment-generations.md) — 接受
- [威胁模型](security.md)
- [内容贡献规范](content.md)
- [运维手册](operations.md)
- [WASI 构建与完整本地体验](wasi.md)
- [生产设置的 HTTPS 验收重放](production-acceptance.md)
- [发布制品构建、校验与导入](releases.md)
- [单主机发布协调、排空与恢复](deployment-coordination.md)
- [验收记录](acceptance.md)

修改核心边界时，先更新 RFC 的候选方案/风险，再追加或替代 ADR；保留旧决策及替代链接。验收记录必须区分单元测试、真实沙箱测试、本地体验和公开部署。
