# ADR-088：保存包围 NVML 的两次完整 XML 样本

日期：2026-11-15。状态：实现与 321 项相关组件检查通过；实际重放越过来源检查，但在已退出成员身份处失败。背景见 [RFC-034](../rfcs/034-gpu-inventory-sampling-window.md)。

观察 schema 3 在同一原三秒期限内固定执行 SMI 版本核对、完整 XML、NVML、完整 XML。`inventory_samples` 保留两个原 XML 的 PID 列表，`pids.inventory` 必须恰好等于它们的并集；NVML 三类列表仍必须是这个完整清单的子集。不能向清单补入仅 NVML 报出的 PID，也不能以第二次为空覆盖第一次的正面记录。

所有来源必须成功、身份与固定制品一致。任一来源失败、诊断输出、冲突或超限都拒绝；没有循环重试、历史 PID 缓存或期限扩展。任务中继续对完整清单逐项读取并复读宿主身份，缺失、变化和外部 cgroup 不豁免。无任务时只要任一样本有 PID，就拒绝空闲结论。

旧 schema 1/2 的解析语义不变；私有 v4 挂载声明和管理制品 v2 不改写。新观察格式来自新执行源码，不是将旧记录升级成新就绪。窗口开始/结束、两个 XML 都缺少 NVML PID、前次外部进程、部分失败、共同期限及原调用/终止边界共 321 项检查通过，日志为 `test-results/gpu-inventory-window-components-2026-11-15-2.log`。

`598ff4e` 的新镜像实际重放失败于已退出成员 PID 651065 的 `state` 检查，正常恢复成功、设备最终空闲；记录在 `test-results/inventory-window-local-diagnostic-2026-11-15/observation-1/`。这次越过来源检查不代表完整启动通过，后续处理见 [ADR-089](089-exited-gpu-process-identity.md)。
