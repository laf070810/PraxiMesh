# RFC-018：GPU 0 的独立隔离环境验证

状态：独立 rootless 环境的单卡识别、真实 CUDA 样例、CPU 节流及清理通过，尚未开放 GPU 评测。日期：2026-10-30。关联 PRD R-16、AC-04、AC-08；前置 [RFC-010](010-specialist-evaluation.md)，决定见 [ADR-017](../adr/017-gpu-environment-probe.md)。

## 本次范围

维护者已完成 rootless Docker 的 CPU 控制器委派，并明确允许独占当前第 0 张 GPU 做验收。四项 Docker 资源能力现均为 true；可信临时容器的 0.5 核配额实测通过。目标卡按 UUID 记录在私有忽略目录，执行前重新核对身份与占用；不使用其他 GPU，不终止未知任务。

官方 gVisor `release-20261026.0` 的完整归档已按 SHA-512 校验并解包到仓库忽略目录；其驱动支持列表包含本机 `590.48.01`。这仅证明版本列表匹配，尚不能证明 RTX 3090、rootless、设备注入及资源回收实际可用。

## 验证方案与边界

- 使用本次创建并追踪的临时 rootless Docker 进程，独立 socket、数据目录、执行目录、PID 文件和配置，不修改或重启运行中站点的 daemon、驱动或 systemd 服务。
- 使用固定版本 runsc，显式启用 nvproxy；不使用允许未支持驱动或强制 ABI 的开关。
- 通过已安装的 NVIDIA 工具生成临时 CDI 配置，只保留目标 GPU UUID 对应的设备项；不使用 `all`。配置仅供临时 daemon 读取，不改系统 CDI 目录。
- 仅运行已审查的基础设施检查和固定来源 CUDA 样例，确认 GPU 身份、可见设备范围、运行结果、CPU/内存/PID 约束与清理。GPU 工作负载必须实际使用 runsc，失败时不回退普通容器。
- 控制器不挂载站点数据或凭证，不让 guest 访问 Docker socket；每项测试有时限。结束后清理本次进程、容器与临时目录，并再次核对既有站点和目标 GPU。

此试验不变更正式评测策略，不生成 sandbox receipt，也不将厂商样例的成功视为学员代码安全或平台 GPU 题型完成。正式 GPU 契约、受保护结果比较、队列/租约、显存与故障预算、成绩证据和完整异常探针仍须后续实现与验收。

## 备选方案

直接修改当前 Docker runtime 配置会影响现有 Compose，故本次不采用。普通 NVIDIA 容器只能证明设备可见，不能代替 gVisor 执行隔离。若当前 rootless 组合无法满足门槛，保留真实失败，再选择独立执行节点；不通过扩大 guest 权限或放宽资源检查来获得通过结果。

第一次临时 Docker 试验通过 daemon 资源能力与 CUDA 镜像获取，runsc 创建容器时返回 `systemd error: Interactive authentication required`，资源已清理。固定版本源码中的 systemd cgroup 驱动调用系统级 D-Bus，未选择用户管理器连接；本次不增加系统级授权。备选验证使用本机已有 Podman，配置独立存储、临时目录、CDI 和相同 runsc，在已委派的用户 cgroup 子树通过 cgroupfs 实施原有资源限额。必须实测限额，不能使用禁用 cgroups 或忽略权限错误的模式。

Podman 备选中的 cgroup 父目录使用本次专属普通子目录，不能直接以 systemd slice 名称作为 cgroupfs 父路径。runsc 不接受 Podman 默认注入的 OCI SELinux process label，因此仅对本次临时容器设置 `label=disable`；主机 SELinux 状态不变，runsc 隔离、只读文件系统、网络关闭、去除 capabilities 与全部资源限额继续保留。该兼容调整仍须实际验证，不作为生产方案自动启用。

进一步验证用户 cgroup 命名空间方案：用本次专属、自动回收的临时用户服务获得独立子树，设置整体 CPU/内存/PID/运行时限，再由现有 RootlessKit 的 `--pidns --cgroupns --evacuate-cgroup2` 将运行时限制在该子树的视图内。先独立验证命名空间和真实 controller 文件，再决定是否进入 GPU 检查。此方案不修改既有用户或系统服务、不更改 runsc 二进制，也不忽略 cgroup 错误。

该路径的实际排查已越过 cgroup 权限和 CDI hook 初始化。128 个宿主线程/进程的初始预算不足以同时容纳 gofer、sentry 和 NVIDIA 辅助程序，内核 `pids.events` 记录拒绝；继续试验采用固定的 256 PID 预算，并限制辅助 Go 程序并发为 2。同时发现容器子 cgroup 的 `cpu.max` 未保持预期值，不能只凭 CLI 参数判定限额生效：可信控制端先在专属 `workloads` 父 cgroup 设置 0.5 CPU、512 MiB 内存、0 swap 和 256 PID，再验证容器继承的有效限制及实际 CPU 节流。该预算用于基础设施试验，尚非正式 GPU 评测策略。

官方依据：[gVisor GPU](https://gvisor.dev/docs/user_guide/gpu/)、[rootless 模式](https://gvisor.dev/docs/user_guide/rootless/)、[NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)。

## 实际结果与下一步

| 检查 | 结果 |
| --- | --- |
| 当前 rootless Docker 的 CPU 限额 | 通过；四项能力为 true，5 秒实测消耗约 2.501 秒 CPU，50 次节流 |
| GPU 0 身份、空闲及驱动列表 | 通过只读预检；UUID 已记录，驱动 590.48.01 在固定 runsc 支持列表中 |
| 独立 Docker + runsc + CDI | daemon 能力检查及固定 CUDA 样例镜像获取成功；runsc 创建系统级 systemd scope 时要求交互认证，guest 未启动 |
| 独立 Podman + runsc + CDI | 保留并排查下载 EOF、cgroupfs 父路径和 OCI SELinux 标签问题；最终 runsc 写 `/sys/fs/cgroup/cgroup.subtree_control` 时权限不足，guest 未启动 |
| 用户 cgroup namespace + 独立 Podman + runsc + CDI | 通过；容器内仅识别授权 GPU 0，真实执行 50,000 元素 CUDA 向量加法，厂商正确性检查输出 `Test PASSED` |
| 该 GPU 路径的实际 CPU 限额 | 最终脚本重放通过；9.002 秒耗费 4.393 秒 CPU，比例 0.488，75 次节流；父 cgroup 的 0.5 核、512 MiB、零 swap、256 PID 限额在执行前后保持一致 |
| 清理及原站点复核 | 初次五轮和后续命名空间试验的专属目录、服务及 cgroup 已清理；共享 Podman 命名空间辅助进程未擅自删除。既有 Web/worker/tutor 健康，回环和直接 IP 健康入口均返回 200，CPU 评测可用；GPU 0 无残留计算进程 |

固定 CUDA 样例镜像为 `nvcr.io/nvidia/k8s/cuda-sample@sha256:6337237230602d2c063263922dc1db6c8ecc8d23c1f8438e172fe912f10f3973`。前两条接入路径只取得镜像；命名空间路径随后实际完成 CUDA 计算。此结果只验证厂商基础设施样例，不包含学员代码、正式成绩或性能比较。

命名空间方案还处理了三个实测兼容问题：为 `/run/lock` 准备私有挂载；覆盖样例镜像继承的 `NVIDIA_VISIBLE_DEVICES=all`，由 CDI 单独按 UUID 选卡；为 Go 辅助程序设置 `GOMAXPROCS=2`，并为完整运行时开销提供固定 PID 预算。探针曾误将 Podman 返回的运行时完整路径与短名称比较，已修正为校验本次固定 runsc 的完整路径并重放。全部原始失败保留。

下一步是将这一基础能力接入 GPU 题型、可信结果比较、设备预约与任务租约，再补齐显存预算、资源超限、取消、worker 失联、故障恢复和性能可比性验收。CPU 委派及基础 GPU 执行路径不再是当前阻塞；没有修改系统级 D-Bus、根 cgroup 权限或现有 daemon。

原始结果保留在忽略目录：`test-results/px-gpu-c21a854521d8/`、`px-podman-gpu-ca97c25f3a7c/`、`px-podman-gpu-bb6747327a26/`、`px-podman-gpu-f00ea72345e6/`、`px-podman-gpu-77b4558be2bd/`；最终状态与清理复核为 `test-results/gpu-runtime-preflight/final-state.json`。初次健康查询受到客户端代理影响返回 502，关闭该请求的代理后两种直连均成功；没有因此修改站点。

后续结果位于 `test-results/px-cgns-57a8b50a3d0e/` 和 `test-results/px-gpu-cgns-*/`。首次完整通过为 `px-gpu-cgns-bc82eca5d489`；整理后的 [检查脚本](../../scripts/gpu_environment_probe.py) 最终实测重放为 `px-gpu-cgns-616bf74c3bca`，脚本 SHA-256、固定 runsc 身份与清理记录见该目录的 `outer.json`，CUDA 与 CPU 结果见 `result.json`。最终全部专属资源清理、既有站点健康和 GPU 0 空闲复核见 `test-results/gpu-runtime-preflight/final-state-after-success.json`。12 项定向单元检查验证设备筛选和未知制品拒绝，不替代这次实测。操作只在 [GPU 部署指南](../deployment/gpu.md#重放单卡环境检查)维护。
