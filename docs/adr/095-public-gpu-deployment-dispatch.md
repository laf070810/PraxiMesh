# ADR-095：公共发布命令选择原 CPU/GPU 生命周期

日期：2026-11-16。状态：263 项组件、冻结制品、真实公共命令与托管 CI 通过，随主分支交付。设计见 [RFC-041](../rfcs/041-public-gpu-deployment-entry.md)。

公共 `apply` 在原协调器锁内完整核验目标归档，再按目标与原状态选择流程。`needs_gpu_lifecycle()` 与原 CPU 拒绝规则共用同一判断，包含当前、前代、候选以及全部 GPU 配置与历史。GPU→CPU 和已退休 GPU 的后续操作仍使用协调流程；仅绑定节点、尚无 GPU 历史的纯 CPU 操作保持原路径。

所选流程需要 GPU 时，必须在取得数据库操作锁前确认明确配置及已完成的原绑定；配置与原实例的后续完整核对继续由原生命周期负责。未知 GPU 恢复记录保持失败，不改用 CPU 恢复。内部 CPU 方法继续拒绝 GPU，状态查询仍不连接数据库。原 CPU/GPU 方法在执行前各自再次核验归档，避免把入口的分派结果变成授权缓存。

分派及原 CPU 检查 90 项通过，合并 GPU 绑定、原库恢复、整站与曝光检查后共 263 项通过。初始旧实现为 35 failed / 3 passed，原日志在 `test-results/public-gpu-entry-before-2026-11-15.log`；最终组件记录为 `public-gpu-entry-components-2026-11-15.log`。这些服务替身、临时文件和 CPU 子进程结果不证明真实 CLI 或硬件部署通过。

真实不同发布包升级和两处中断矩阵完成后，源提交 `3193c35` 在原独立站点通过公共 `apply/recover`：GPU 完整启动、曝光调用 SIGKILL、保留原 worker 启动及新增写入、普通正确 GPU Submit，以及正常 GPU→CPU 和新 CPU Submit。55 张业务表、33 个序列保留；现场失败与复验分别记录在[站点验收](../acceptance-gpu-site.md#public-command-success)。该提交的应用与完整 WASI 托管工作流也通过。完整 GPU Compose 与目标主机仍须分别验证，不提供强制激活或跳过检查的参数。
