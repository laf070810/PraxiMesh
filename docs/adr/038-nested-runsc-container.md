# ADR-038：用独立配置验证容器内的直接 OCI runsc

日期：2026-10-31。状态：接受开发验收路径；GPU 产品部署未完成。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)。

默认只读监督容器不能创建嵌套用户命名空间，也不能操作子 cgroup。保留宿主 RootlessKit/Podman 路径，新增独立的 `RuntimeContainerService` 和直接 OCI `ContainerRuntime`，在不占 GPU 的条件下验证容器化运行基础。新服务的预约身份范围、镜像源码契约和完整 syscall 配置均与普通监督容器区分，不能用一类配置接管另一类。

可信外层仍由 rootless Docker 运行，UID/GID 为 65532，无额外能力、无主机挂载、无网络或设备，根目录只读。使用固定 Moby 配置快照，仅额外允许 `clone`、`unshare`、`mount`、`umount2`、`pivot_root`、`setns` 和 `chroot`。这些调用用于 runsc 的私有命名空间和 gofer 根目录；不使用 privileged 或 unconfined，不改共享 daemon。保留上游许可证与摘要，配置发生变化必须重新审阅和实测。

完整 runsc 归档先在私有构建目录核对 SHA-512，再包含其 sidecar。guest 根文件系统来自固定 Python 镜像，外层控制器代码和状态不进入 guest。直接调用 `runsc --rootless` 的单 UID 路径；guest UID 0 位于内部虚拟环境，能力为空，不是宿主 root。根目录只读、网络关闭，临时目录有 32 MiB 上限，关闭 directfs。源码命令只能由可信平台调用者构造，不能把学员输入插入命令行。

外层 2 CPU、2 GiB、无 swap、512 任务上限约束监督器及整个运行时。这不同于宿主路径的独立 guest 子 cgroup；不在 OCI 配置里填写未经实施的 guest CPU/内存上限。生产接入仍须确定版本化资源契约，实测资源故障与总预算，并更新完整启动检查和凭证。

普通命令超时、附着客户端退出和 guest 清理分开验证。最外层 PID 1 的绝对期限仍覆盖 runsc、gofer 和 Sentry；最终验收还要观察 Docker 实际停止及已记录进程的启动身份消失。新路径尚未连接 GPU worker、设备注入、可信控制通信、准入或发布协调器。操作与验证入口统一在 [GPU 指南](../deployment/gpu.md#container-runtime)，结果见 [G22](../acceptance-g22.md)。
