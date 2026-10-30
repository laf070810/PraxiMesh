# G6：GPU 控制连接与占用监测组件

日期：2026-10-30。范围为运行适配器的连接组件，**尚未形成完整 GPU worker，未执行 GPU 工作负载**。适配器设计见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#受信运行适配器)。

新增 `judge/gpu_transport.py`：私有目录中的本机 socket、内核提供的对端 UID/PID、任务 cgroup 归属检查、4 MiB 帧/8 MiB 累计预算、期限、断连与额外输出拒绝。只在两个受信控制器之间传递数据；该通道不是 guest 的执行隔离。源码与受保护测试的实际传输需待运行适配器接入，不写入命令行或任务文件。

新增 `judge/gpu_monitor.py`：按完整设备 UUID 检查身份，读取包含计算、图形及其他占用的 XML 进程清单，只有属于自有任务 cgroup 的进程可继续；未知 PID/身份或外部占用均拒绝，不终止未知进程。进程类型含义见 [NVIDIA 文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html#processes)，内核对端凭据见 [Unix socket 文档](https://man7.org/linux/man-pages/man7/unix.7.html)。

`test-results/g6-transport.xml` 为 **74 passed**，包括真实本机 socket 大消息、内核对端凭据、超时/断连/重复字段拒绝、合成占用与既有服务/会话回归。它没有验证跨 RootlessKit 命名空间的实际连接，也不证明 GPU 成功。另用只读 NVML 查询核对本机驱动的 XML 格式，`g6-monitor-readonly.json` 记录目标卡仍有 1 个进程；未启动 CUDA，也未改变占用。

新增模块纳入 GPU 执行摘要，CPU 策略未变化。后续需接通私有隔离环境、服务、连接、会话和恢复，再进行真实 GPU 验收；本轮不生成 receipt 或打开提交入口。维护者新增的编辑器与页面排版改进先进入实现，GPU 完整目标保留。
