# RFC-035：已退出但仍可完整读取的 GPU 成员身份

日期：2026-11-15。状态：成员身份调整与 328 项相关检查通过，真实 CUDA 重放待完成；决定见 [ADR-089](../adr/089-exited-gpu-process-identity.md)。

`598ff4e` 的原状态重放不再停在上一轮的来源一致性位置，而是在 GPU PID 归属读取中失败。私有诊断明确记录 `ProcessDisappeared`、PID 651065、字段 `state`；对应源码只在读取到 `Z/X/x` 状态时产生此结果。该分支已验证 PID、出生时间、NSpid 和 cgroup，进程目录并非缺失。原宿主采样也将此 PID 关联到原隔离任务。普通恢复成功，记录为 `test-results/inventory-window-local-diagnostic-2026-11-15/observation-1/`。

当前 `HostProcessView.read()` 同时承担存活检查与成员归属读取，导致 GPU 上下文结束后的短暂已退出成员也被拒绝。新增 `read_observed()` 只用于当前 NVIDIA 列表的成员身份：它必须完成两次完整读取，PID、出生时间、NSpid 和 cgroup 一致，并属于原任务。已退出状态不使完整身份失效，但不证明 GPU 空闲。

原 `read()`、任务 PID 1、容器运行状态、journal 和前后核对仍要求原有存活条件。真正缺失、读取失败、只剩单次身份、PID 复用、迁移或外部 cgroup 均继续失败。不得利用诊断用的 `requested_pid` 或 `field` 补造身份，也不缓存过去的 PID、以空结果重试或放宽期限。返回的 NVIDIA PID 仍保留在完整清单中，任务清理与设备空闲另行确认。

先验证完整已退出成员可归属，以及 PID 1 退出、成员消失、复用和迁移仍拒绝，再使用新匹配镜像重放原固定程序。完成实际 CUDA 和服务器验收前不开放提交。
