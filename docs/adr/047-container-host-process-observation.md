# ADR-047：控制容器中的宿主进程归属

日期：2026-10-31。状态：接受；十八项真实无 GPU 观察/编译/恢复场景通过，原控制端兼容重放进行中，见 [G31](../acceptance-g31.md)。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)。

GPU 工具返回宿主 PID，控制容器的私有 `/proc` 不能直接解释它。新增显式挂载契约 v2，在原控制端身份之外保存宿主 procfs dev/inode 与 boot ID；将该视图递归只读、rprivate 挂到固定位置。控制端仍使用私有 PID/cgroup，维持原 UID 映射、无额外能力、只读根和资源限制。v1 继续不启用此接口。

只读 procfs 不限制所有魔术链接的目标，因此只交给已有专用 rootless Docker socket 权限的可信控制端。代码只打开固定的 stat/status/cgroup 文件，使用有界读取和进程目录描述符；不读取环境、命令行、内存、fd 或 root，不向任务容器和 guest 暴露该挂载，也不在观察中发送信号。

监督器在完整配置核对后返回 Docker 报告的 PID 1。观察对象绑定原持锁 journal、不可变容器 ID、启动实例、PID 1 出生时间、其所在的实际 Docker cgroup 和宿主 PID 视图；cgroup 路径按不透明字符串比较，不解析其中的 `..`。观察前后重新核对容器、原记录、进程出生时间与 cgroup；退出、复用、迁移、挂载变化或读取失败均拒绝。任务清理后原观察对象失效，不能用于新任务。

镜像源码清单包含观察接口及完整驱动制品契约，控制器可以独立验证后续任务镜像。原持久任务 journal v1/v2 格式不变，控制端挂载清单 v2 是另一个独立版本。共享监督源码变化使 GPU 执行摘要更新；CPU/WASI 策略未变，旧 GPU receipt 不能作为当前代码的凭证。

此接口只提供进程归属判断，不提供 NVIDIA 进程清单、不证明 GPU 空闲或授予预约/就绪。单卡注入、NVIDIA PID 来源、正式环境、完整 CUDA 启动和容器 worker/Compose 仍须继续接入和硬件验证。操作保留在 [GPU 指南](../deployment/gpu.md#container-process-observer)。
