# ADR-049：容器完整设备进程来源

日期：2026-10-31。状态：接受；十九项真实管理/CPU/恢复场景、实际 v3 查询兼容及 475 项组件检查通过，范围见 [G33](../acceptance-g33.md)。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)，原上下文接口见 [ADR-048](048-container-nvml-observation.md)。

固定三个 NVML 上下文 API 无法单独证明没有其他 GPU 资源占用。选择同一 NVIDIA 安装的完整 XML 进程查询作为补充来源，保留原生 NVML 设备身份与三类列表。`Processes` 的类型定义见 [NVIDIA 官方说明](https://docs.nvidia.com/deploy/nvidia-smi/index.html)；解析不按类型过滤，其他、组合或未知类型的 PID 均必须核对归属。

管理制品新增显式 schema 2/profile `nvidia-inventory-v2`，包含原 NVML 文件、固定 SONAME 相对链接和 `/usr/bin/nvidia-smi` 快照；每个文件绑定大小、SHA-256 和静态 ELF 检查，来源只允许不可由普通用户改写的系统安装。运行时再次校验清单、链接、文件及当前内核。工具版本与驱动版本必须一致，管理镜像缺少 CUDA 库时允许仅 `CUDA Version` 为 `N/A`。源码不包含这些 NVIDIA 二进制，也不据此改变其授权。

显式控制端契约 v4 只接受此制品，v3 只接受原上下文制品。保留原私有 PID/cgroup、只读宿主进程视图、同一设备锁和 journal；只映射选定卡与 `nvidiactl`，不增加 UVM、能力、主机 PID 模式或新宿主挂载。

一次观察共用一个三秒绝对期限：固定 NVML 子进程、固定 SMI 版本查询、指定 UUID 的完整 XML 查询。命令和环境均固定，SMI 仅从制品目录查找 NVML。完整输出上限为 1 MiB，在可信控制端解析后只保留 PID；不记录进程名称、显存、原始 XML 或其他私有元数据。

全部来源成功且身份一致后才返回。缺失/重复关键字段、N/A 进程列表、非法/重复 PID、上限错误、警告或命令失败均关闭观察；不会生成空列表回退。三个 NVML 列表必须是完整列表的子集；采样间进程变化可能导致暂时拒绝，不能把不一致解释为空闲。空列表仍须核对原任务锚点，非空列表的每个 PID 都必须属于原实例。

完整查询没有授予设备排他权，也不是硬件恢复或正式就绪证明。当前仍需真实非空 CUDA/其他/无上下文进程来源和持续任务归属验收，再连接任务设备与完整 worker。操作集中在 [GPU 管理检查](../deployment/gpu.md#container-device-observer)。
