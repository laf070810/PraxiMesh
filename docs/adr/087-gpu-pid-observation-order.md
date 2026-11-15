# ADR-087：完整 PID 采样处于同一次归属核对内

日期：2026-11-15。状态：实现与 293 项相关组件检查通过，首轮实际 CUDA 重放仍失败，继续诊断。背景和原失败见 [RFC-033](../rfcs/033-gpu-pid-observation-order.md)。

`OwnedProcesses.observe_owned()` 先完整核对原任务，再调用固定管理查询，立即读取全部返回 PID 的身份并复读，最后重新核对原任务。它复用现有的 cgroup、出生时间、PID 1、容器实例、journal 和控制器检查。`ContainerDeviceMonitor.observe()` 通过这个入口完成一次有原对象约束的采样，消除查询后、读取 PID 前的重复容器核对。

直接 `require_owned(pids)` 与受控终止观察保持原语义。任何缺失、来源冲突、两次身份不同、外部 cgroup 或原对象变化仍失败；空列表也必须经过完整前后核对。没有跨查询缓存、消失 PID 豁免、重新归属、重试续期或预算扩容。

短生命周期时钟模型验证旧顺序在额外检查后失去进程，新顺序可在进程仍存活时核对其身份；这个模型不证明硬件时序。相关归属、管理、受控终止、调用链、取消和控制器检查共 293 项通过，日志为 `test-results/gpu-pid-order-components-2026-11-15-2.log`。必须使用新匹配镜像重放原本地状态与服务器启动，不能复用旧凭证或将组件结果算作 GPU 通过。

`07fb091` 的匹配镜像随后沿原状态重放，仍在启动初期返回平台不可用；普通恢复成功。只读记录中的 NVIDIA PID 333933 属于原任务，NSpid 为 `[333933, 44, 1]`。该结果说明顺序调整不足以解决当前失败，不能据模型确认根因。记录为 `test-results/pid-order-local-diagnostic-2026-11-15/observation-1/`。

为继续定位，本地入口在仍持原 journal 锁时追加 `local-failure.<nonce>.json`，只保存固定异常类别、最多四个冻结平台源码位置，以及消失 PID 的数字和固定读取字段；对应的原控制器记录用于关联镜像。它不保存错误正文、源码、局部变量或配置，不覆盖旧文件，写入失败不遮盖原异常。`ProcessDisappeared.identity` 的归属语义不变，新增诊断属性不授予归属或清理权限。211 项相关诊断和原入口检查通过，日志为 `gpu-pid-diagnostics-components-2026-11-15-2.log`；须重建匹配镜像后再次观察。

`47bdfe2` 的实际诊断随后确认失败于 NVML/SMI 来源一致性检查，普通恢复成功；后续处理见 [ADR-088](088-gpu-inventory-sampling-window.md)。PID 读取顺序和双重身份核对继续保留。
