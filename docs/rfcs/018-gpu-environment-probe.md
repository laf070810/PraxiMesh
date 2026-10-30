# RFC-018：GPU 0 的独立隔离环境验证

状态：验证中，尚未开放 GPU 评测。日期：2026-10-30。关联 PRD R-16、AC-04、AC-08；前置 [RFC-010](010-specialist-evaluation.md)。

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

官方依据：[gVisor GPU](https://gvisor.dev/docs/user_guide/gpu/)、[rootless 模式](https://gvisor.dev/docs/user_guide/rootless/)、[NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)。
