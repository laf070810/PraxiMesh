# RFC-032：GPU 管理查询的进程命名空间

日期：2026-11-15。状态：版本化启动契约已接入，715 项相关组件、25 项实际管理/CPU 容器和 20 项 worker/本地入口检查通过，见 [ADR-086](../adr/086-gpu-observer-pid-namespace.md)。新冻结版本的 CUDA 重放尚未完成，GPU 公共激活保持关闭。

## 问题与证据

`50d90b5` 的独占 GPU 验收通过后，GPU 0 出现其他账号的 Slurm 进程。宿主完整 SMI 查询列出这些进程，但原可信 worker 内固定 NVML 三类查询和完整 SMI 均返回空列表。两种来源在同一受限命名空间中一致，不足以证明设备空闲。两个 worker 崩溃验收尝试均未实际注入故障；原任务和失败记录保留，自有 GPU worker 已正常停止。

在同一不可变 worker 镜像、同一驱动与管理库、同一 GPU 上进行只读对照，没有执行 CUDA：

| 控制器配置 | 宿主查询前后 | NVML / SMI |
| --- | --- | --- |
| 私有 PID、默认 `/proc` | 相同的 14 个 PID | 均为空 |
| 私有 PID、宿主 `/proc` 递归只读挂载 | 相同的 14 个 PID | 均为空 |
| 宿主 PID、默认 `/proc` | 相同的 10 个 PID | 均完整列出这 10 个 PID |

第三组较晚运行，外部作业数量已变化；每组只比较自己的查询前后集合。自有短时容器均已按完整 ID 清理。原始记录分别在 `test-results/gpu-inventory-visibility-2026-11-15/` 和 `test-results/gpu-inventory-visibility-2026-11-15-host-pid/`。结论适用于本次 RTX 3090、590.48.01 驱动与 rootless Docker 环境；不把其他驱动的行为当作已验证。

## 调整边界

首先在管理查询前验证：当前可信控制器必须与原私有清单固定的宿主 procfs 使用同一个 PID 命名空间。通过固定 `self` 路径读取控制器自身的 stat/status，核对当前 PID、单层 NSpid 和出生时间；不能将容器内数字 PID 1 当作宿主 PID 1。缺失、嵌套、错配、读取失败均拒绝查询，不能返回空闲。

后续只为固定可信 GPU 控制器启用宿主 PID：协调器 worker、本地 GPU 控制器和对应验收入口分别维护创建契约。Web、无设备预检、编译容器、任务外层及 runsc guest 保持原独立 PID 语义。可信控制器仍使用原 rootless UID 映射、能力全撤销、no-new-privileges、只读根、私有 cgroup、原资源限额和固定宿主 procfs；不增加任意宿主执行、未知 PID 信号或设备选择能力。

新创建记录版本化保存 PID 模式；旧记录继续按原冻结参数核对并允许停止/退役，不能改写旧记录或重建同名对象。旧制品可核验不代表可按新协议启动，发布能力只能从原归档字节确认。管理查询修复改变执行身份，不能沿用旧就绪凭证。

真实 CPU 对照首次在启动前发现 Docker 自动增加 `label=disable`，原精确配置检查因此拒绝，原报告保留。独立的只创建检查确认默认 seccomp、capabilities、资源限额、masked/readonly 路径仍在。当前 daemon 的安全选项只有默认 seccomp、rootless 和 cgroupns；宿主 SELinux enforcing 不代表这个 rootless daemon 启用了容器 SELinux 隔离。新版 host PID 配置只接受 daemon 未启用 SELinux 且保留默认 seccomp 的情形，遇到启用的 SELinux 策略直接拒绝，不自动关闭。Docker 的追加逻辑见其[官方创建实现](https://github.com/moby/moby/blob/master/daemon/create.go)。这个限制须同时用于验收与后续生产创建/启动契约。

## 验证与开放条件

组件检查覆盖私有命名空间误判、伪装为宿主 PID 1、缺失或多重 NSpid、身份变化、旧创建记录和新契约篡改。真实 CPU 容器验证命名空间拒绝、原任务仍为私有 PID、控制器清理及原锁归属；管理查询对照验证外部进程可见，不创建外部 GPU 工作负载。

完整 GPU 启动、正常 API、worker 崩溃恢复与不同冻结版本的升级须等待原卡独占后重放。已有独占条件下的成功结果保留其原范围；不能声称它们证明了外部占用检测。未完成上述检查前，公共 GPU 激活保持关闭。
