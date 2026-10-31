# G33：容器完整设备进程来源

2026-10-31。范围为本地管理制品 v2、控制契约 v4、完整 XML 进程来源与原任务归属。按维护者“先继续不占 GPU 的开发”要求，只读取明确选定的 GPU 0 管理元数据，不加载 CUDA 库、创建计算上下文、运行内核、停止未知进程或修改共享主机。任务仍是无 NVIDIA 设备的固定 CPU guest。

实现与边界见 [ADR-049](adr/049-container-full-device-inventory.md)、[RFC-021](rfcs/021-gpu-container-supervision.md)；命令只维护在 [GPU 部署指南](deployment/gpu.md#container-device-observer)。

## 结果

- **十九项真实镜像检查通过**：十项设备/库/工具/驱动/挂载/版本配置拒绝，私有 PID 控制端的完整管理查询与原 CPU 任务归属，原 v2 进程视图兼容，以及七组 v1 身份、双向锁与跨容器/宿主恢复。原报告为 `test-results/px-container-controller-dd747ed6fc68/result.json`，日志为 `g33-full-controller.log`。
- **实际 v3 查询兼容通过**：相同当前控制源码、原 NVML-only 制品及私有命名空间成功读取身份和三类列表。该项使用固定 Web 基础镜像与只读暂存源码的诊断容器，不是第二次十九项固定镜像重放；日志为 `g33-legacy-diagnostic.log`。
- **475 项组件检查通过，无跳过**：完整列表和类型、版本/制品/链接校验、同一截止时间、失败关闭、原任务锚点、v1–v4 挂载兼容、运行时/监督/编译/驱动及发布兼容。原始记录为 `g33-components.log`、`g33-components.xml`。此前 160 项定向检查为重叠范围，不另行累加。
- 整仓 Ruff 和格式检查通过，记录为 `g33-ruff.log`、`g33-format.log`。十一份相关文档的 376 个本地文件链接通过；新增文档后重放 36 项发布兼容检查通过，属于前述 475 项的重叠范围。记录为 `g33-document-links.json`、`g33-release-docs.log`。

本次所选卡为 NVIDIA GeForce RTX 3090，驱动 `590.48.01`、计算能力 8.6、字符设备 minor 0。宿主完整列表、容器 NVML 三类列表和容器完整列表均为空；`real_external_gpu_processes_observed: false`。这证明此次身份与空列表查询成功，不能据此声称已验证真实 CUDA、其他或无上下文进程的来源、任务归属与排他性。

## 原始失败与修正

临时可信容器首次查询被版本解析拒绝：管理镜像没有 CUDA 库，`nvidia-smi --version` 报告 `CUDA Version: N/A`，而工具、NVML 和驱动版本均可正常读取。保留两次失败的诊断 JSON、`g33-diagnostic.log` 和 `g33-diagnostic-version.log`。修正只允许这个非身份字段为 `N/A`；工具和驱动仍精确匹配，NVML 版本不能缺失。

修正后独立管理诊断通过，记录为 `g33-diagnostic-fixed.log`；随后重新构建并通过上述十九项完整镜像检查，没有用诊断镜像替代最终固定制品。初次诊断启动脚本曾在子容器失败时返回零，但其报告明确为 `passed: false`，不计为通过；后续脚本已把此情况转为失败退出。静态检查发现的导入排序及夹具变量遮蔽也已修正。

## 身份与清理

| 身份 | 本次值 |
| --- | --- |
| 控制源码契约 | `347e9cc6b3cb117978199115930335c33357d1376c7b3913bdf601251dd9e808` |
| 完整管理制品清单 | `b69d3c279f0df752b87722478d21b20b424e265fbb43a137c8c6c5172962cf33` |
| 控制镜像 | `sha256:0257b0c9c565ebbacbbe3a3f88e139a30a92ecfa197c927498ef6544ed451bb1` |
| CPU 任务镜像 | `sha256:d3a70f8ab5852c9ad53501934428eab6f12d129974dd85070fc906ed9557883b` |

原 runtime、compiler、GPU execution 和 CPU/WASI 策略摘要与 G32 一致；本次只改变控制来源契约，不签发新的 GPU receipt。完整库/工具大小与摘要、逐源码摘要保存在原报告和 `g33-audit.json`，本机 NVIDIA 二进制不提交或公开分发。

只读最终审计确认本批报告记录的 **33 个容器 ID、2 个镜像标签、5 组原进程**已清理；其中包括四个独立管理诊断容器。临时控制目录及诊断目录不存在，实际验收源码与报告一致，现有 Web、WASI worker 和 tutor 均健康。记录为 `g33-audit.json`、`g33-audit.log`，没有清理未知对象或重启现有服务。

完整列表读取和原任务核对已接通，但真实非空 GPU 来源、任务单卡隔离、CUDA 故障恢复、正式环境/启动凭证和完整 worker/Compose/发布仍未完成。静态夹具中的 `O` 类等案例只验证解析与拒绝规则，不替代硬件验收；继续按[需求追踪](requirements.md)保留完整产品目标。
