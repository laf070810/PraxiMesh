# G13：控制器故障、独立终止与新调用恢复

日期：2026-10-31。设计：[RFC-019](rfcs/019-gpu-kernel-evaluation.md#正式就绪检查)、[ADR-029](adr/029-gpu-controller-lifecycle-checks.md)。本轮继续不占 GPU。

## 实际 CPU/runsc 结果

`test-results/px-gpu-lifecycle-e7ded67e9d01/result.json` 的三项检查全部通过，总 `passed`、`temporary_directory_removed` 为 `true`。命令与预期字段集中在 [GPU 指南](deployment/gpu.md#不占-gpu-的控制器故障检查)。

| 固定场景 | 实际观察 | 后续恢复 |
| --- | --- | --- |
| 控制器直接退出 | 从服务启动前计时约 10.951 秒观察 EOF；显式停止前已核对整 cgroup 为空 | 遗留映射存储回收，新服务/新 CPU 容器运行通过 |
| 通道失联 | 约 11.031 秒观察 EOF，服务仍存活；由外层停止原调用 | 遗留映射存储回收，新服务/新 CPU 容器运行通过 |
| 独立硬期限 | 约 25.144 秒观察 EOF，显式停止前已核对整 cgroup 为空；内层未轮询或自行退出 | 遗留映射存储回收，新服务/新 CPU 容器运行通过 |

上述时间包含环境准备，不能视为故障响应延迟或 GPU 性能。每项故障服务使用 35 秒外层期限，systemd 的运行预算扣除已有 10 秒启动/停止余量；共享观察代码拒绝过早断开。三项都实际触发映射存储回收，使用同一持久维护身份、新的 `InvocationID` 和新容器恢复。固定 guest 只执行基础设施程序，不加载 NVRTC、映射设备或接收学员源码。最终自有服务、回收助手、工作目录和临时根均已核对清理。

GPU 适配器已连接同一握手、故障注入与终止观察；新调用的正确解恢复等待原调用和目录清理成功。GPU 维护总预算最多 180 秒，故障/文件回收与正确解恢复分别受限。实际 CPU 脚本使用自己的故障和恢复预算，不冒充 GPU 全路径时限验收。

## 组件与加载检查

- 初次定向测试因参数名 `request` 与 pytest 保留名冲突，在收集阶段退出；记录保留于 `g13-lifecycle-initial.log/xml`。更名后的定向检查为 144 passed，见 `g13-lifecycle-component.log/xml`。
- 最终相关回归为 247 passed、无跳过，见 `g13-lifecycle-final.log/xml`；覆盖握手、调用身份、提前退出、额外消息、故障清理、预算、固定 CUDA 用例、会话和映射存储回收。
- 全仓 Ruff 及 460 个 Python 文件的格式检查通过。
- `test-results/px-gpu-controller-74d923cb9ca2/result.json`：新增模块后的实际控制器加载、认证及准备前拒绝通过，自有资源清理完成。此检查未准备 GPU 运行环境。
- GPU 执行摘要为 `0ec26f3bbdf4b10310f688ba44eea25fb811770754e9d7f62392fae42b874eef`。CPU 策略仍为 `ae5cfea16ee6c6fa5cc76b5a8356fc3e30c27bfe8a86de27c8456ca55c2111c8`。

## 验证边界

新的 CUDA 会话保持、故障终止、独立正确解恢复和总预算尚未在硬件上运行。本次没有生成 GPU 凭证、改变设备 ready 状态、运行正式 GPU worker 或开放提交；也未改变数据库、用户站点、共享服务或驱动。完整启动编排、凭证、健康准入及产品接入仍待完成。

未重跑 PostgreSQL、WASI、Compose 或浏览器矩阵；此次未修改相应路径。README 中的现有用户能力和启动入口继续有效。
