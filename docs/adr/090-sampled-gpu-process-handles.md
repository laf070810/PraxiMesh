# ADR-090：以查询内进程句柄确认采样成员的退出

日期：2026-11-15。状态：实现与 CPU 检查通过，实际 CUDA 重放待完成。背景与原失败见 [RFC-036](../rfcs/036-sampled-gpu-process-handles.md)。

完整来源路径通过 `OwnedProcesses.observe_samples()` 在原任务前后核对之间采样。每次完整 XML 返回后立即核对其成员的完整身份、原 cgroup 和 Linux pidfd；`fdinfo` 的 PID/NSpid 必须对应当前宿主视野。后续正面样本仍须重新读取身份，不能借前次句柄认领已消失或复用的数字 PID。

最终核对允许本次已完整绑定、随后消失的成员，仅当原 pidfd 明确报告整个进程组退出；读取错误、权限不足、仍活着但缺少元数据、身份变化或不明确的 poll 事件均拒绝。所有句柄在本次调用退出时关闭。它们不跨查询保存、不用于发送信号，不取代原任务 PID 1、容器、journal 或租约核对。XML 并集、NVML 子集和三秒共同期限不变；无任务的空闲观察及最终清理也不变。

真实 CPU 子进程验证了句柄绑定、存活、退出、回收、错配与关闭；还覆盖来源失败、并集改变、原任务改变和下一次查询不得复用句柄。宿主原 cgroup 中的 systemd `user@UID.service` 作为原样字符串比较，增加合法 `@` 字符识别，不把 cgroup 当路径访问。共 345 项相关检查通过，日志为 `test-results/gpu-sampled-handles-components-2026-11-15-3.log`。测试中的只读挂载及完整容器身份各有原专项检查；此次实际 CPU 进程证据不代表新的 GPU 完整启动或服务器成绩通过。

`cbe8734` 的匹配镜像先通过无设备 CPU 句柄检查，但实际 GPU 重放在 44.446 秒因句柄命名空间比较失败，普通恢复成功。原检查误将目标进程的 `fdinfo.NSpid` 限制为单层；原 GPU 进程身份实际为 `[1099833, 44, 1]`。两个独立、无设备 CPU 容器的前后对照确认 `status` 与 `fdinfo` 都包含目标完整链，新检查逐项匹配该链，不能省略层级。修复后原句柄绑定、自然退出确认及容器清理通过；351 项相关检查通过，日志为 `test-results/gpu-sampled-namespace-components-2026-11-15-1.log`。证据在 `test-results/sampled-handles-local-diagnostic-2026-11-15/` 的 `cpu-image-check/`、`observation-1/`、`nested-cpu-before/`、`nested-cpu-after/`；后两项使用实际宿主检查代码与 CPU 容器进程，不冒充新镜像的 GPU 验收。
