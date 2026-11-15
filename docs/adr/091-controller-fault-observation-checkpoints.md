# ADR-091：EOF 后共享检查时钟并读取新状态

日期：2026-11-15。状态：原失败复现、371 项相关检查和实际 CPU 故障矩阵通过；完整 GPU 重放待完成。背景见 [RFC-037](../rfcs/037-controller-fault-observation-checkpoints.md)。

故障观察继续把完整 `state()` 回调交给传输层。EOF 后及终止等待循环先调用 `Channel.check()`，沿用原 0.25 秒检查时钟，再读取新的原容器状态；不能把上一回调的状态作为最终退出证据。检查时钟到期仍执行完整租约、设备及原记录回调，新的状态读取继续核对锁、socket、原 ID、完整监督参数、OOM 与原绝对期限。

没有扩大硬期限、五秒结束窗口或总预算，也没有时间容差；不主动停止容器来完成独立期限检查。故障后的原 journal 清理与新容器正确恢复保持原路径。

带四秒完整回调的测试在原代码上复现同一个 EOF 后期限异常，日志为 `test-results/gpu-fault-clock-reproduction-2026-11-15-1.log`。修复后，新状态读取、时钟到期必须重新检查、EOF 后容器替换或错误退出，以及原预算浮点边界和相关进程/传输检查共 371 项通过，记录为 `test-results/gpu-fault-clock-components-2026-11-15-1.log`。这些耗时是明确模型，不是 CUDA 测量；实际 CPU 与新匹配双镜像的完整 GPU 重放仍分别验收。

随后标准 `container_lifecycle_acceptance.py` 五组实际 CPU/runsc 检查全部通过：控制器退出、通信断开、独立期限、准备失败和新恢复失败。源码核对、原进程树消失、正确性恢复及临时目录/镜像标签清理通过；没有访问 GPU。原报告为 `test-results/px-container-lifecycle-aa1cb3a4edea/result.json`，调用日志为 `test-results/gpu-fault-clock-cpu-acceptance-2026-11-15-1.log`。该模块同时属于控制器和任务源码契约，后续必须重建匹配双镜像，不能继续借用旧任务镜像。
