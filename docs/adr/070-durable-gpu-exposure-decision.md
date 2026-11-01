# ADR-070：先保存数据库决定，再开放 GPU 站点

日期：2026-11-02。状态：采用，内部曝光串接；完整 GPU 发布仍未开放。延续 [RFC-022](../rfcs/022-gpu-release-coordination.md) 和 [ADR-069](069-recoverable-gpu-candidate-preparation.md)。

`scripts/deploy_gpu_exposure.py` 只接受已完成的原候选准备。进入前核验完整冻结包、规范归档路径、原操作、发布代和已清理的验证轮次；重新观察两类 worker、原暂停和维护路由，不能以持久 verified 代替当前健康。

在恢复准入之前，同一次原子日志写入保存 `current`、`exposure_target` 和 `gpu_exposures` 中的决定。决定绑定完整发布代、操作、归档、验证轮次、原 GPU 容器/启动/owner/环境及 Web 端口。后续阶段为 resumed、activating、active；代理持久配置的写入也必须晚于决定。从决定开始，失败或响应丢失都保留当前数据库，不再选择旧快照。

开放后的只读观察允许原暂停或开放状态，但拒绝其他 token 和不一致的暂停字段。原实例、配置、冻结环境及新鲜性仍逐次核对；正式冻结 Web 的 `judge_admission status` 进一步确认 CPU/GPU 可用、任务兼容、预约租约和预算。合法用户排队与运行不要求被清空；预约使用实际 UUID。最后重新观察数据库与原实例，并计入环境检查的耗时，外部健康必须明确确认两类评测可用。

此观察分离的是只读身份核对，不放宽普通准备、配置发布和 worker 创建/启停的原暂停及维护路由要求。数据库观察不写状态或释放预约；曝光步骤不创建候选检查、不重启 worker、不复制或清理数据库。已确认开放后再次被暂停或撤回路由时要求原库恢复，不自动重新打开。

当前代在维护状态完成原库恢复和新的候选验证轮次后，可建立新的曝光记录；此前决定和用户写入保留。旧启动记录不能为新启动背书。公共 `apply/recover` 的 GPU 拒绝仍覆盖曝光日志；完整初始安装、升级失败回退及 GPU→CPU 仍需连接并完成真实环境验收。

结果与边界见 [G54](../acceptance-g54.md)，操作与检查入口集中在[部署指南](../deployment/container-gpu-worker.md#durable-exposure)。
