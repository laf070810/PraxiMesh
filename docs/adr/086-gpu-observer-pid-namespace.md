# ADR-086：管理查询必须证明完整宿主 PID 视野

日期：2026-11-15。状态：版本化启动契约已接入，715 项相关组件、25 项实际管理/CPU 容器和 20 项 worker/本地入口检查通过；新冻结版本的 CUDA 启动与恢复仍待重放。背景见 [RFC-032](../rfcs/032-gpu-observer-pid-namespace.md)。

固定 NVML 和完整 SMI 在同一私有 PID 命名空间中都可能漏报外部进程。两者结果一致不能取代命名空间检查；读取失败或视野不足时必须拒绝，不能产生空闲结论。

`HostProcessView.require_same_pid_namespace()` 在管理库或设备检查之前，从原固定宿主 procfs 的 `self` 目录读取两次自身 stat/status/cgroup。当前 PID、出生时间、单层 NSpid 与原挂载身份必须一致。不会用容器数字 PID 读取宿主同号进程，也不把观察结果转为向未知进程发送信号的权限。

可信 GPU 控制器采用宿主 PID 模式，保持 rootless 用户映射、能力全撤销、默认 seccomp、no-new-privileges、私有 cgroup 和原配额。Docker 自动追加的 `label=disable` 仅在 daemon 本来未启用 SELinux 容器隔离时可接受；启用 SELinux 或缺少默认 seccomp 则拒绝。Web、无设备检查、编译、任务容器和 runsc guest 不采用此模式。

协调器的新 GPU worker 使用 schema 3 创建记录，本地控制器使用 schema 2；宿主 PID 参数及 Docker 的精确安全选项纳入原运行摘要。无设备预检仍使用 schema 2 和私有 PID。镜像携带 `io.praximesh.controller.pid-observer=host-pid-v1`，新发布另外从已核验归档解析 `PID_OBSERVER_PROFILE`，不执行归档源码、不仅凭镜像标签授予新协议能力。Compose 只对 `gpu-worker` 声明 `pid: host`。

旧创建记录不改写，继续按原参数、ID 和运行摘要停止或退役。新协调器拒绝启动旧私有 PID worker，也不接受它的历史 ready 记录用于候选或曝光；本地旧控制器只可清理。新创建及启动前检查 daemon 安全策略，容器入口还会独立核对；不能用旧制品或凭证冒充新 GPU 执行身份。完整启动、任务、清理与恢复仍须通过新冻结镜像实测，公共激活保持关闭。

组件覆盖自身 PID、嵌套/缺失 NSpid、读取失败、身份变化及安全模块条件。实际容器验证私有 PID 拒绝、宿主 PID 通过、原 UID/锁/挂载核对，以及三个方向的原任务恢复；没有映射设备或查询 NVIDIA。最终源码摘要匹配，全部自有容器、临时存储和镜像标签清理通过，报告为 `test-results/px-container-controller-dcac45757bc3/result.json`。首次配置拒绝保留在 `px-container-controller-50089ec9631b/`；组件日志为 `gpu-pid-guard-components-2026-11-15-2.log`。

配套入口随后完成 715 项组件检查，包括实际归档的协议读取、旧记录只清理、PID 模式变化拒绝，以及旧 ready 不能用于新曝光。扩展首轮的测试夹具缺少安全选项，补齐后 44 项配置检查通过；初始失败日志继续保留，结果分别在 `gpu-pid-runtime-components-2026-11-15-4.log` 和 `gpu-pid-runtime-config-2026-11-15-2.log`。

实际管理查询完成 25 项检查，报告为 `px-container-controller-0864d32d585d/result.json`。宿主与可信容器的完整列表一致，控制器 PID 相同，原任务的 NSpid 仍以私有 PID 1 结束；v2 CPU 观察、原锁与恢复兼容通过。本轮观察时设备空闲，不将它当作新的外部占用正例。上一轮因旧断言仍要求控制器 PID 1 而失败，原报告 `px-container-controller-9ec2664e355d/` 保留；修正后分别检查控制器和任务的真实命名空间。

实际 worker 镜像、Compose 和 PostgreSQL 的十项无设备检查通过；本地控制器和客户端各五项实际 CPU 检查通过，包含退出、取消、SIGKILL 与原对象恢复。源码摘要一致、自有资源清理及原服务不变均确认，汇总为 `test-results/host-pid-cpu-acceptance-2026-11-15/result.json`。此外，新代码只读核验原临时站点两个已停止的 schema 2 对象，原部署文件字节未变，记录为 `gpu-pid-retired-original-records-2026-11-15.json`。这些结果不生成 GPU receipt，也不证明新版本 CUDA 启动通过。
