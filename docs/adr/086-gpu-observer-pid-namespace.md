# ADR-086：管理查询必须证明完整宿主 PID 视野

日期：2026-11-15。状态：管理查询拒绝边界已实施，168 项组件和 12 项真实 CPU 容器检查通过；版本化启动契约仍待接入。背景见 [RFC-032](../rfcs/032-gpu-observer-pid-namespace.md)。

固定 NVML 和完整 SMI 在同一私有 PID 命名空间中都可能漏报外部进程。两者结果一致不能取代命名空间检查；读取失败或视野不足时必须拒绝，不能产生空闲结论。

`HostProcessView.require_same_pid_namespace()` 在管理库或设备检查之前，从原固定宿主 procfs 的 `self` 目录读取两次自身 stat/status/cgroup。当前 PID、出生时间、单层 NSpid 与原挂载身份必须一致。不会用容器数字 PID 读取宿主同号进程，也不把观察结果转为向未知进程发送信号的权限。

可信 GPU 控制器采用宿主 PID 模式，保持 rootless 用户映射、能力全撤销、默认 seccomp、no-new-privileges、私有 cgroup 和原配额。Docker 自动追加的 `label=disable` 仅在 daemon 本来未启用 SELinux 容器隔离时可接受；启用 SELinux 或缺少默认 seccomp 则拒绝。Web、无设备检查、编译、任务容器和 runsc guest 不采用此模式。

当前增量先拒绝不完整观察；协调器和本地控制器随后使用版本化创建记录接入该模式。旧创建记录不改写，保留按原身份停止和退役；不得用历史凭证启动新契约或宣称新 GPU 执行身份已通过。只有相应真实启动、任务、清理与恢复检查完成，才考虑开放提交。

组件覆盖自身 PID、嵌套/缺失 NSpid、读取失败、身份变化及安全模块条件。实际容器验证私有 PID 拒绝、宿主 PID 通过、原 UID/锁/挂载核对，以及三个方向的原任务恢复；没有映射设备或查询 NVIDIA。最终源码摘要匹配，全部自有容器、临时存储和镜像标签清理通过，报告为 `test-results/px-container-controller-dcac45757bc3/result.json`。首次配置拒绝保留在 `px-container-controller-50089ec9631b/`；组件日志为 `gpu-pid-guard-components-2026-11-15-2.log`。
