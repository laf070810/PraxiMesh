# ADR-093：受控退出也使用同次查询身份

日期：2026-11-15。状态：实现、组件与实际 CPU 检查通过，完整 GPU 重放待完成。背景见 [RFC-039](../rfcs/039-terminating-task-sampled-identities.md)。

只有固定维护故障完整触发后，`KernelTask` 才传入原观察绝对期限。完整来源观察在同次查询开始时绑定原 PID 1，并在各 XML 样本返回时绑定成员；正面清单保留，原句柄只用于确认本次已绑定进程的退出。普通执行、直接归属检查以及首次无法绑定的成员仍按原严格条件失败。

原进程退出、Docker 状态尚未落定时，只等待原完整 ID/启动实例和原 journal，在原硬期限加既有五秒结束窗口内确认停止。传入期限不能扩大、刷新或变成非有限值。未绑定的原 PID 1 消失不授予成员归属；只能等待原实例停止后读取完整空清单。已确认退出后再做一次完整来源查询，并复核原实例仍停止；新占用、权限错误、替换、重用或状态不明均失败。等待不停止任何进程、不循环查询 GPU 到空，不持久缓存句柄或退出结果。

原代码上的采样后退出复现保留在 `test-results/termination-window-reproduction-2026-11-15-1.log`。391 项相关检查通过，包括实际 CPU pidfd、回收与关闭、明确的 Docker 延迟模型、首次缺失、外部/后续占用、期限和旧调用边界，记录为 `test-results/termination-window-components-2026-11-15-2.log`。

实际 CPU/runsc 五组故障矩阵也通过。额外四次原实例观察使用真实 journal、Docker、CPU 进程及 pidfd，独立期限场景在一次短采样内跨越实际退出，随后确认原实例停止与空清单；原进程树、临时目录及镜像标签清理通过。证据为 `test-results/termination-window-cpu-2026-11-15/result.json` 和 `test-results/px-container-lifecycle-5f87315f541a/result.json`。其中进程清单是固定 CPU 样本，不是 NVIDIA 数据；宿主 procfs 查询也不代替容器只读挂载契约检查。这些结果不能签发 GPU receipt。
