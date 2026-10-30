# ADR-020：GPU 任务服务硬期限与有界控制回调

状态：接受组件设计，2026-10-30。已用固定 CPU 进程树和真实 PostgreSQL 验证控制组件，尚未接入正式 GPU worker。背景见 [RFC-019](../rfcs/019-gpu-kernel-evaluation.md)，结果见 [G5](../acceptance-g5.md)。

## 决定

每个任务使用独立临时用户服务，服务名和描述绑定操作 UID、设备 UUID、任务 UUID 和预约令牌。持锁控制器核对服务身份、调用 ID、cgroup、限制和日志配置后才操作；重启后的控制器可以按数据库保留的预约找到旧服务。未知同名服务与发生替换的服务实例均拒绝停止。此机制不把同 UID 的恶意主机进程排除在信任边界外。

使用 `Type=exec`、`Restart=no`、`KillMode=control-group`、SIGKILL、5 秒启动和停止期限。整卡预约剩余时间中保留 10 秒用于启动及终止，其余作为 `RuntimeMaxSec`；内层会话仍受 G4 的更小期限约束。服务管理器负责运行期限和整个进程树终止，不依赖 worker 心跳及时返回。该语义依据 [systemd 服务文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#RuntimeMaxSec=)及[终止文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html#KillMode=)，并在本机 systemd 252 重放。

本机不支持通过临时服务设置 `JobTimeoutSec`。因此固定入口 `scripts/gpu_service_entry.py` 在执行可信控制器之前检查启动的绝对 monotonic 截止时间，排队过久则直接退出。运行定时器在该入口运行之前已启动；符合门槛的启动时间加运行上限与停止余量不会获得新的预约时段。任务参数不得包含学员源码、答案或密钥；源码仍仅通过已验证的 guest 协议传递。

服务总预算为 2 核、2 GiB、零 swap、512 PID。启动后读取实际 cgroup 文件核对限制，关闭时确认服务停止且整个 cgroup 没有进程；CPU 配额值读取与固定进程树终止不替代 CUDA、显存或设备恢复验收。日志输出设为 null，可信控制器须另行保存有界、私有、无源码的状态记录。

`core/gpu_control.py` 提供独立短事务回调：PostgreSQL 锁等待 250 毫秒、每条语句 1 秒；SQLite 在事务开始前暂设 250 毫秒 busy timeout，结束后恢复原值。截止时间检查覆盖数据库操作前后，跨越任务时段则回滚更新。服务端等待限制与[数据库语句/锁超时](https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-LOCK-TIMEOUT)不等同于网络调用硬期限，因此独立任务服务期限仍是必要条件。

控制回调没有执行隔离或硬件恢复权限。受信运行适配器只有确认自有工作负载停止、设备空闲、正确性恢复通过后，才能使用 `finish_after_recovery` 或 `release_after_recovery`。数据库不可达、回收失败或外部占用时保留预约并停用；不能仅凭服务退出自动释放 GPU。

## 交付边界

组件和固定验收入口可在源码开发环境运行，已纳入 GPU 执行摘要；当前 CPU Compose 镜像、发布协调器和 Web 准入不调用它们。还需将私有 rootless 容器环境、会话驱动、外部占用监测和恢复探针接入同一任务服务，再完成启动 receipt、产品入口与真实 GPU/部署验收。
