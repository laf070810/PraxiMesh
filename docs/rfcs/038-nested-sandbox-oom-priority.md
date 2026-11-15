# RFC-038：嵌套 sandbox 的 OOM 淘汰优先级

日期：2026-11-15。状态：调整、351 项相关检查和三组实际 CPU 资源检查通过；GPU 重放待完成，见 [ADR-092](../adr/092-nested-sandbox-oom-priority.md)。

`7b2b7a7` 的新双镜像通过初始恢复，但资源检查在 126.222 秒断开。原任务 `d80799ee…` 于 11:52:59.884087、11:53:08.202010 UTC 出现两次 OOM，随后以 137 退出；原硬期限约为 11:54:52.180807。普通恢复完成、无活动记录、GPU 0 空闲。原事件在 `test-results/fault-clock-local-diagnostic-2026-11-15/observation-1/task-events.json`，不能把整个任务被杀算成资源检查成功。

独立标准 CPU 资源矩阵三组通过，未访问 GPU。只读采样显示，原 512 MiB 总预算下，Sentry、guest 子进程、可信控制器和 PID 1 均继承 `oom_score_adj=200`。第一次 OOM 结束高 RSS 的 guest 子进程后，约 455 MB shmem 仍短暂留在原 cgroup；CPU 路径随后完成原清理。证据在 `test-results/resource-memory-diagnostic-2026-11-15/`。该观察不能单独证明 GPU 第二次 OOM 的全部原因，但说明杀掉一个 guest 子进程并不立即释放 sandbox 持有的全部共享内存。

固定 runsc [对应发布源码](https://raw.githubusercontent.com/google/gvisor/release-20261026.0/runsc/container/container.go) 支持 OCI `process.oomScoreAdj`，在启动后调整 sandbox 和 gofer 的分数。仅为本程序创建的私有 runsc guest 固定为 1000，提高它们相对可信监督进程的淘汰优先级；不降低宿主或监督进程分数，不修改未知 PID，不增加 capability、挂载权限或任何配额。

CPU、内存、进程数、原绝对期限和清理要求不变。整个任务 OOM、来源/清理不明或后续正确恢复失败继续拒绝成功报告。旧任务仍按原冻结制品清理，不修改历史资源 profile 或凭证。先用同一无设备采样核对实际进程分数、OOM、内存释放与独立恢复，再重建匹配镜像验证真实 CUDA；不能只凭 OCI 字段或静态测试声称修复完成。
