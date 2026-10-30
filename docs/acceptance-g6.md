# G6：GPU 控制连接与占用监测组件

更新：2026-10-31。范围为运行适配器的连接组件，**尚未形成完整 GPU worker，本批未执行 GPU 工作负载**。适配器设计见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#受信运行适配器)。

## 2026-10-30：传输与占用监测

新增 `judge/gpu_transport.py`：私有目录中的本机 socket、内核提供的对端 UID/PID、任务 cgroup 归属检查、4 MiB 帧/8 MiB 累计预算、期限、断连与额外输出拒绝。只在两个受信控制器之间传递数据；该通道不是 guest 的执行隔离。源码与受保护测试的实际传输需待运行适配器接入，不写入命令行或任务文件。

新增 `judge/gpu_monitor.py`：按完整设备 UUID 检查身份，读取包含计算、图形及其他占用的 XML 进程清单，只有属于自有任务 cgroup 的进程可继续；未知 PID/身份或外部占用均拒绝，不终止未知进程。进程类型含义见 [NVIDIA 文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html#processes)，内核对端凭据见 [Unix socket 文档](https://man7.org/linux/man-pages/man7/unix.7.html)。

`test-results/g6-transport.xml` 为 **74 passed**，包括真实本机 socket 大消息、内核对端凭据、超时/断连/重复字段拒绝、合成占用与既有服务/会话回归。它没有验证跨 RootlessKit 命名空间的实际连接，也不证明 GPU 成功。另用只读 NVML 查询核对本机驱动的 XML 格式，`g6-monitor-readonly.json` 记录目标卡仍有 1 个进程；未启动 CUDA，也未改变占用。

新增模块纳入 GPU 执行摘要，CPU 策略未变化。这次增量没有生成 receipt 或打开提交入口；之后维护者要求的编辑器与页面排版改进已完成，见[编辑器验收](acceptance-editor.md)，GPU 完整目标保留。

## 2026-10-31：实际跨命名空间连接

`judge/gpu_namespace.py` 将预约工作目录、环境变量白名单、离线 RootlessKit 启动及受限 workload 父组连接到既有任务服务。目录重新打开与清理须核对预约身份、权限、inode、服务停止、cgroup 无进程及挂载边界；部分清理失败保留身份。决定见 [ADR-022](adr/022-gpu-controller-namespace.md)，重放命令统一在[部署指南](deployment/gpu.md#不占-gpu-的控制链检查)。

实际结果为 `test-results/px-gpu-namespace-943be41a5b4d/result.json`，**5 项通过**：

- 外层与内层通过私有路径 socket 传输 753,740 字节，仅以摘要核对返回；合成私密标记未出现在命令行或工作目录文件中。
- 断连及额外消息均使固定控制器退出，独立进程组中的子进程随后也被任务服务清理；未主动调用 stop 即观察到整个 cgroup 为空。
- 控制器停止轮询时，服务独立期限为 5.990357 秒，从检查开始约 6.097 秒确认进程树终止。
- 同 UID、不同任务 cgroup 的真实连接被拒绝；随后正确服务的连接通过内核凭据检查。

五项均读取到外层 2 核、2 GiB、零 swap、512 PID，以及内层 workload 父组 0.5 核、512 MiB、零 swap、256 PID。七种命名空间与外层不同，网络只有回环接口；六个刻意注入的无效环境变量未进入内层。每项 `cleanup.cgroup_empty` 和 `workspace_removed`、总 `temporary_directory_removed` 均为 `true`。这些是固定 CPU 控制进程的实际检查，没有启动 Podman guest、NVML、CUDA、编译器或 GPU 恢复探针。

`test-results/g6-namespace-final.xml` 为 **95 passed、无 skipped**，覆盖工作目录替换、权限、符号/硬链接、FIFO、挂载拒绝、部分失败保留、控制传输、占用解析和既有服务/会话回归。连接重置现统一返回平台失败，不作为干净完成。

保留的初次失败：`px-gpu-namespace-af343eaa9cbe/result.json` 中 RootlessKit 因白名单 PATH 不含 `ip` 所在目录而退出 127，随后控制连接超时；只补入固定系统目录，没有继承整套外层环境。`px-gpu-namespace-b3b33f86770e/result.json` 中额外消息已使进程退出，但验收错误地要求正常 EOF，内核因未读数据返回 reset；修正为分别核对拒绝后的进程终止和正常结果的完整 EOF。中间完整通过记录为 `px-gpu-namespace-a1ad9fdd3857/result.json`，新增挂载清理保护后再次完成上述最终重放。失败服务也已清理，失败记录保留。

最终 GPU 执行摘要为 `5b068abb753579282fe9458c202f53ba2c07499af81e9ef21b6ed09390fca53a`；CPU 策略仍为 `0f471fa8819325d70a04366283d448aefaa885d6ebf88b0b7c9ac467bcc28a53`。未修改运行站点、共享 daemon、GPU 驱动或主机服务配置。后续仍须连接离线 Podman/runsc 会话、占用监测、恢复及正式 worker，再完成独占 GPU 和产品验收；当前没有 GPU receipt，提交保持关闭。
