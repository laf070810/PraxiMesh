# ADR-089：完整已退出成员身份与存活检查分开

日期：2026-11-15。状态：实现与相关组件检查通过，真实 CUDA 重放待完成。背景见 [RFC-035](../rfcs/035-exited-gpu-process-identity.md)。

`HostProcessView.read_observed()` 只用于当前 NVIDIA 清单成员的归属核对。它保留两次完整 stat/status/cgroup 读取与挂载检查；仅当 `process_identity()` 已验证完整身份、随后因 `Z/X/x` 状态拒绝存活时，允许继续比较原身份。PID、出生时间、NSpid 和 cgroup 必须一致，成员必须属于原任务，清单读取后仍复读全部成员并核对原任务。

原 `read()`、原任务 PID 1、直接 `require_owned()` 和受控终止继续要求存活。真正缺失、部分读取、复用、移动和外部已退出进程仍拒绝。此读取不证明进程存活、容器退出或设备空闲；正面 NVIDIA 清单不会被删除，不使用历史缓存或诊断字段补造身份。

相关进程、来源、终止、调用链与故障检查共 328 项通过；包括三种退出状态、读到一半被回收、出生时间变化、cgroup 变化、外部已退出成员与原 PID 1 退出。日志为 `test-results/gpu-exited-member-components-2026-11-15-2.log`。新源码仍须构建匹配镜像并完成实际 CUDA 启动，不能据此开放网站提交。
