# G7：离线运行环境与容器回收

日期：2026-10-31。范围为共用 Podman/runsc 运行模块、实际固定 CPU guest 和清理路径，**本批未访问 GPU、加载 CUDA 或开放 GPU 提交**。设计见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#受信运行适配器)及 [ADR-023](adr/023-gpu-offline-runtime.md)，操作统一在[部署指南](deployment/gpu.md#不占-gpu-的离线容器检查)。

`judge/gpu_artifacts.py` 集中固定 runsc、OCI 图和 NVRTC 校验，旧探针继续调用；`judge/gpu_environment.py` 提供独立离线存储、固定运行时、启动前核对、CUDA 会话入口和自有容器回收；`judge/gpu_command.py` 限制控制命令的输入、输出、期限和取消等待。新的 CUDA 分支尚未在 GPU 上重放，不以 CPU 检查代替其硬件验证。

## 实际固定 CPU 容器

最终原始记录为 `test-results/px-gpu-runtime-5e3a114ceed6/result.json`，总用时约 38.789 秒，**七项通过**。每项之后创建新的隔离容器，重新核对合成数据摘要；十四个容器均确认停止并删除，私有存储、任务 cgroup 和工作目录已清理。

| 检查 | 实际结果 |
| --- | --- |
| 身份、文件、网络和设备 | guest UID 为 65534，capabilities 为零；根目录写入被拒绝；控制器私有文件不可见，宿主回环端口不可达，没有 NVIDIA 设备节点 |
| 输出超限 | 大量未分帧输出被限制；终止实际容器后，新容器正常交换数据 |
| 超时 | 已确认启动的 guest 进入等待后触发 1 秒请求期限；实际容器终止及重新运行通过 |
| 取消 | 等待期间触发取消，仍完成容器检查、终止和重新运行 |
| 附着客户端退出 | 杀死本次附着 CLI 后，实测容器仍在运行；会话清理独立终止它，随后重新运行通过 |
| CPU 限额 | 工作负载用时约 3.001 秒、消耗 1.500 秒 CPU，出现 28 次节流，0.5 核上限生效 |
| 临时存储 | `/tmp` 写入达到 32 MiB 后收到空间不足，清理及重新运行通过 |

此次在 `nvproxy=false`、无 GPU 映射、完全断网的 RootlessKit 环境内执行。它证明上述 CPU 隔离与控制路径；没有证明新的 CUDA 会话、显存预算、GPU 正确性、性能或设备恢复。

最终制品身份：

- runsc：`release-20261026.0`，SHA-256 `7d658f2c3a5602d28c391285eb4ee0740e7dd21d40731324049a29620e51cf5e`，完整归档校验覆盖 sidecar。
- Python OCI 清单：`sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15`；镜像 ID `sha256:f0c05afecbd16040caff4c000954567c7e3b56fc6c1f783fa10a55cba3ccfbfc`。
- 本次运行使用 Podman 5.8.2，实际运行源码摘要与最终工作树对应模块一致。GPU 执行摘要为 `918f12db90364fa83dec0c53a2bb11fbb91960f04d313d76d2d2d680c049ed2f`；它还不是正式启动 receipt。
- CPU 策略仍为 `0f471fa8819325d70a04366283d448aefaa885d6ebf88b0b7c9ac467bcc28a53`。

## 其他检查与原始失败

`test-results/g7-runtime-final.xml` 为 **164 passed、无 skipped**，覆盖制品拒绝、固定输入快照、有界控制管道、取消与期限、容器身份/参数拒绝，以及既有 namespace、service、session、transport 和 monitor 组件。加入独立 home 后，`px-gpu-namespace-cf8019ec7de7/result.json` 的五项实际控制连接复验也通过。

`test-results/g7-nvrtc-artifacts.json` 只验证固定 NVRTC 归档的解包、库摘要、别名、驱动文件及私有 umask 下的目录/文件权限，临时目录已删除；没有加载库、编译或启动 CUDA。这个检查覆盖了从探针抽取制品逻辑后的文件准备路径。

保留了两类实际失败及修复：

1. `px-gpu-runtime-38a5ffcf3674/result.json`：镜像库在缺少子进程 home 时访问 `/root/.config/containers`，启动失败。修正为隔离子进程独有的空 home，同时继续使用显式私有配置和存储；共享用户环境没有改动。
2. `px-gpu-runtime-ae111f1bc59b/result.json`：身份/隔离检查通过，输出超限被发现，但容器清理在满的附着管道下超时。`GPUSession.close` 改为先关闭并终止自有 CLI，再无条件核对、终止和等待自有容器；失败未作为正常判题结果保留。

首次完整通过为 `px-gpu-runtime-b884986a6e95/result.json`。随后补齐 CUDA 分支与原探针一致的驱动能力声明，在最终源码上重放得到上述 `5e3a114ceed6` 记录。失败时的服务、存储和工作目录也已清理；失败记录保持原样。

后续仍需接入外层设备占用监测、单卡执行与独立正确解恢复、异常重启后的运行状态恢复、数据库结算及正式 worker；完成真实 GPU、队列、准入和部署验收后才开放题型。整体 PRD 目标继续保留。
