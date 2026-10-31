# G38：容器资源与隔离启动检查

2026-10-31。九项固定资源检查接入容器维护协议，报告绑定原请求、目标设备、整任务预算和原 guest 清理记录。设计见 [ADR-054](adr/054-container-resource-startup-checks.md)、[RFC-021](rfcs/021-gpu-container-supervision.md)；操作集中在[无设备调用链检查](deployment/gpu.md#container-kernel-flow)。本轮没有查询、映射或占用 GPU。

## 实现与验证范围

固定 guest 检查只读根、外层私有文件和回环不可达、零能力、禁止提权及最小环境。CPU 节流、OOM 和进程上限使用实际整任务 cgroup 计数；`/tmp` 写入须触发空间限制。取消及附着退出先观察原 guest 与附着进程活跃。每项都在新 guest 中确认恢复，共保留十七个原 guest 的清理记录；完整资源检查后另起正确内核恢复，再按原 journal 清理外层任务。

CPU 固定程序不加载 CUDA 驱动，不编译或执行学员源码。生产 GPU 路径另校验 CUDA 枚举的唯一 UUID、型号和架构，以及原物理设备节点；这些硬件分支只有明确的组件替身验证，本轮没有运行。报告不等同 GPU receipt、显存预算或正式 worker 验收。

## 原始失败与修正

首次完整入口 `test-results/px-container-kernel-3be65b77b9fa/result.json` 通过十五个既有场景后，在新增资源请求的内存探针清理中出现 `Private runtime command exceeded its deadline`。该场景从创建到归档约 42 秒，没有耗尽 180 秒总期限。它完成前十四个 guest 的清理；内存 guest 在后续关闭中也已确认停止/移除，成功报告没有返回。外层原容器、进程树、镜像标签和临时存储均已清理，原日志为 `g38-kernel-first.log`。

新增固定夹具的受限调用位置诊断和 `--resources-only` 专项后，`px-container-kernel-6f089c34a93f/result.json` 的三个场景通过，记录为 `g38-resource-diagnostic.log`。九项资源、十七个原 guest 清理及正常恢复通过；故意让后续正确恢复失败时，报告被扣留，随后新普通调用成功。实际 CPU 为约 3.000 秒内使用 1.496 秒、节流 30 次；临时存储 32 MiB，OOM kill 增加 1，253 个固定子进程使任务数达到 256，清理后恢复为原来的 3 个任务。

专项没有重现首次超时，原报告不足以追溯到具体 runsc 子命令。代码复查发现主动 OOM 分支未先退出仍活跃的原附着进程，和正常关闭顺序不同；补齐终止及等待后，再执行原 guest 和完整进程树清理。保持原命令超时、总预算和失败拒绝规则，没有把重试成功当作首次失败不存在。154 项对应组件检查通过，记录为 `g38-oom-order.log`、`.xml`；附着退出无法确认仍不能返回内存超限成功。

## 最终重放

修正后 **1,124 项相关回归通过，无跳过**，记录为 `g38-components-final.log`、`.xml`；覆盖原任务用途、目标设备与测量值拒绝、OOM 清理顺序、宿主兼容、完整启动/准入/worker 的既有边界和发布兼容。先前 1,050 项相关组件、191 项初始专项、183 项宿主/容器报告边界检查分别记录在 `g38-components`、`g38-focused-first`、`g38-resource-boundaries` 日志和 JUnit 中，范围重叠，不累计为独立验收数。

**十八项真实 CPU/runsc 场景全部通过**，报告为 `test-results/px-container-kernel-cbed33325d0b/result.json`，日志为 `g38-kernel-final.log`。包括原有十六项调用/固定启动检查，以及新增资源成功和后续恢复失败；最终新普通调用也通过。资源报告只有在九项检查、十七个 guest 清理及独立正确恢复后返回；失败恢复场景没有返回成功报告。最终资源测量为约 3.001 秒使用 1.497 秒 CPU、节流 30 次、32 MiB 存储上限、1 次 OOM kill；253 个固定子进程使任务数达到 256，清理后恢复为原来的 3。

**七组真实无设备控制镜像检查通过**，报告为 `px-container-controller-a625545d7989/result.json`，日志为 `g38-controller-final.log`。覆盖用户映射、共享锁、错误配置拒绝、跨控制器/宿主原记录接管及镜像内完整源码策略。没有启用 NVIDIA 观察或连接应用数据库。

收尾只读审计 `g38-audit.json`、`g38-audit.log` 确认本轮失败、专项和最终矩阵的 **52 个自有容器、5 个镜像标签、40 组原进程身份**均已清理，临时存储已移除。最终两份通过报告的源码摘要与当前文件一致，原失败和旧专项保留各自身份。既有 Web、WASI worker 和 CPU 辅导服务均健康。Ruff、639 文件格式检查、差异检查通过；九份相关文档的链接和章节锚点检查记录在 `g38-docs.json`。

## 策略与制品

| 身份 | 当前值 |
| --- | --- |
| 容器运行契约 | `6fe7fc2018a175fc4339849d791714d8af2fce48c443d2f628b6fd82736d165f` |
| 可信控制器源码契约 | `cd5725aa2172b0a74503ba6a04ebcb4a07bdcb80486031a8e85206558cea21be` |
| 单卡任务源码契约 | `3f9169227a9844b137d4e8ca78166ebaae6b76f6ada7f0d40fe18031e7413177` |
| GPU 执行策略 | `f2b3b7556d767acbb20fc45da0409c65699dfeaca2a1200133b889c3587a8e85` |
| 无设备内核验收镜像 | `sha256:4411c9dc848f9852200c0c294ed555c57c6d84f7745fd49f48a8111d63aabc87` |
| 无设备控制镜像 | `sha256:28d90bb7b0072317ae13a9882467552bb9d575abea58dd7ab62887099ac5ad8e` |

新资源 guest、测量和校验源码均纳入运行契约、GPU 执行策略及控制镜像。旧 GPU receipt 不能复用于新源码。CPU/WASI 策略保持 `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9`；本次没有重跑 WASI 探针、PostgreSQL、Compose 或浏览器矩阵，也没有数据库迁移或内容发布。

整控制器故障、完整启动发布、worker/Compose/发布协调仍待接通。实际设备绑定、非空 GPU 进程来源、CUDA 正确性/性能、候选内容和完整队列仍待授权及硬件验收；网页 GPU 提交保持关闭。已有站点未重建，本轮不替换正在运行的编辑器或页面镜像。
