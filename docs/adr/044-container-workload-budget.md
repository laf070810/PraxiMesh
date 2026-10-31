# ADR-044：容器任务的版本化总预算

日期：2026-10-31。状态：接受；完整容器 GPU worker 尚未交付。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)，操作集中在 [GPU 指南](../deployment/gpu.md#container-workload)。

新增固定 `container-workload-v1`，以整个任务容器为计量和限制单位：0.5 CPU、512 MiB 宿主内存、无 swap、256 个宿主任务。预算包括 PID 1、可信通信、runsc 及 guest；guest 的 `/tmp` 另有 32 MiB 上限。此方案不需要向可信容器开放可写 cgroup、额外能力或宿主目录。它不表示 guest 独享 512 MiB，也不限制或证明 GPU 显存容量。

原开发配置冻结为 `container-supervision-v1`，保留 2 CPU、2 GiB、无 swap、512 个宿主任务。只允许可信服务选择已知版本，不接受任意数值。创建参数、实际 Docker 配置、PID 1 实读 cgroup、运行时和启动握手均须一致；预算标识绑定监督启动参数和持久意图，不因重连或恢复改变。

新预算使用 journal v2 的显式 `resource_profile` 字段；v1 的字段和原预算保持不变。恢复根据原记录选择固定版本，按原镜像、参数和实例停止并归档；不执行旧镜像或用新预算解释旧记录。错配、未知版本或缺失字段保留记录并拒绝清理，数据库预约仍由原拥有者核对流程管理。

内存超限由可信外层在会话中持续读取内核 `memory.events`，要求本 cgroup 的 `max`、`oom` 和 `oom_kill` 均有新增，再确认原 guest 与进程树清理后分类；不等待附着客户端自行退出。内核的 `oom_kill` 也包含全局 OOM，因此未知退出码或单独 kill 计数不足以证明任务超限，定义见 [cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)。整任务 OOM 可中断监督进程，外部观察端须保留原容器的实际 OOM/退出状态并确认整棵进程树停止。固定 PID 探针只在经过核对的私有任务容器内运行平台内置 fork 代码，不接受学员源码，也不把 gVisor 的虚拟 guest 进程数等同于宿主线程数。

验收覆盖真实 CPU 节流、内存/PID/存储上限、故障后新容器编译，以及两种预算的持久恢复和数据库预约；NVRTC 仍只能在 gVisor guest 中处理源码。后续正式容器执行环境必须记录预算 ID 和“包含监督及运行时开销”的范围，并重新完成硬件与启动验收，不能沿用宿主子 cgroup 的环境身份或旧 GPU receipt。
