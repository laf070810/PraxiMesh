# RFC-018：GPU 0 的独立隔离环境验证

状态：CPU 与版本预检通过，两条 GPU 容器接入均未通过，尚未开放 GPU 评测。日期：2026-10-30。关联 PRD R-16、AC-04、AC-08；前置 [RFC-010](010-specialist-evaluation.md)。

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

官方依据：[gVisor GPU](https://gvisor.dev/docs/user_guide/gpu/)、[rootless 模式](https://gvisor.dev/docs/user_guide/rootless/)、[NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)。

## 实际结果与下一步

| 检查 | 结果 |
| --- | --- |
| 当前 rootless Docker 的 CPU 限额 | 通过；四项能力为 true，5 秒实测消耗约 2.501 秒 CPU，50 次节流 |
| GPU 0 身份、空闲及驱动列表 | 通过只读预检；UUID 已记录，驱动 590.48.01 在固定 runsc 支持列表中 |
| 独立 Docker + runsc + CDI | daemon 能力检查及固定 CUDA 样例镜像获取成功；runsc 创建系统级 systemd scope 时要求交互认证，guest 未启动 |
| 独立 Podman + runsc + CDI | 保留并排查下载 EOF、cgroupfs 父路径和 OCI SELinux 标签问题；最终 runsc 写 `/sys/fs/cgroup/cgroup.subtree_control` 时权限不足，guest 未启动 |
| 清理及原站点复核 | 五次试验的临时目录、专属 cgroup 和可归属进程均无残留；共享 Podman 命名空间辅助进程未擅自删除。原 Web/worker/tutor 健康，回环和直接 IP 健康入口均返回 200，CPU 评测可用；GPU 0 无计算进程 |

固定 CUDA 样例镜像为 `nvcr.io/nvidia/k8s/cuda-sample@sha256:6337237230602d2c063263922dc1db6c8ecc8d23c1f8438e172fe912f10f3973`。镜像被获取不等于 CUDA 程序已执行；本轮没有取得真实 GPU 计算成功的证据。

下一步属于运行时适配和产品实现：在已委派的用户 cgroup 范围内实施真实资源限制，处理 runsc 的 rootless cgroup 路径/管理器兼容性，再重放设备范围、正确性、超时和清理检查。CPU 配置已修复，不应继续要求维护者扩大系统级 D-Bus 或根 cgroup 权限，也不能设置忽略 cgroup 错误来绕过。

原始结果保留在忽略目录：`test-results/px-gpu-c21a854521d8/`、`px-podman-gpu-ca97c25f3a7c/`、`px-podman-gpu-bb6747327a26/`、`px-podman-gpu-f00ea72345e6/`、`px-podman-gpu-77b4558be2bd/`；最终状态与清理复核为 `test-results/gpu-runtime-preflight/final-state.json`。初次健康查询受到客户端代理影响返回 502，关闭该请求的代理后两种直连均成功；没有因此修改站点。
