# ADR-069：可中断恢复的 GPU 候选准备

日期：2026-11-02。状态：采用，内部完整候选准备；实际 GPU 和整站发布待验收。延续 [RFC-022](../rfcs/022-gpu-release-coordination.md)、[ADR-065](065-original-database-gpu-worker-recovery.md) 和 [ADR-068](068-gpu-candidate-verification-rounds.md)。

候选库完成迁移、最小权限和准入暂停后，用 `scripts/deploy_gpu_candidate.py` 串接配置发布、Web/CPU 服务、独立预检重置与执行、GPU worker 创建/启动、原实例就绪、CPU 队列检查和 GPU 轮次检查。每一阶段使用已有严格组件，不提供宿主执行、普通容器或模拟生产路径。

`gpu_preparations` 保存原操作、完整发布代、规范绝对归档路径及准备尝试。进入流程前重验完整冻结包和 v2 协议；更改服务名、归档路径、暂停归属、实际维护路由或曝光目标时拒绝。阶段先后与原启动绑定，未知记录不续跑；verified 每次仍重查 CPU/GPU 就绪，并匹配精确设备、运行契约及已清理的验证轮次，不是曝光许可。

恢复继续使用当前私有配置的原库。先确保原 CPU worker 可完成遗留 CPU 工作，再经既有 GPU 原库恢复执行新预检和完整启动。只有对应恢复记录 ready、原实例新鲜就绪后，才清理未完成的旧验证轮次，保存新的准备尝试，重建无状态 Web、启动 CPU worker 并重新检查两类队列。清理失败保留原尝试，不创建新轮次；中断重试沿原恢复记录继续，不重复启动 GPU worker。

最多保留 16 次准备尝试；历史、原数据库、任务和预约不被删除。服务生命周期、预检、GPU 启动和提交继续使用各自原来的限时，不通过重试重置。该流程不迁移或复制数据库，不切换代理，不恢复对外准入；完整 apply、曝光决策、失败回退和 GPU→CPU 仍由后续整站协调完成。

CPU `apply` 的验证位置也使用同一 `prepare_candidate` 入口，保留原行为。公共 GPU 激活拒绝继续生效，并包含新的准备状态。检查结果及真实/模型边界见 [G53](../acceptance-g53.md)，入口集中在[部署指南](../deployment/container-gpu-worker.md#candidate-preparation)。
