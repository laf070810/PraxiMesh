# G34：单卡设备配置与持久恢复边界

2026-10-31。新增容器任务的单卡配置、嵌套 OCI 设备映射与 journal v3。**设备 guest 尚未成功启动，不能将本次视为 GPU 隔离、CUDA 正确性或完整部署验收通过。** 设计见 [ADR-050](adr/050-single-card-container-task.md)、[RFC-021](rfcs/021-gpu-container-supervision.md)，操作只维护在 [GPU 指南](deployment/gpu.md#container-device-nodes)。

## 实现与已验证范围

单卡配置固定完整 UUID、驱动、所选卡 minor 与内核实际注册的 UVM major；Docker 和 OCI 只从同一配置生成所选卡、控制与 UVM 三个节点。新镜像契约与无设备驱动镜像分开，保持原只读根、私有 PID/网络、seccomp 和任务总预算。控制端仍只挂管理节点，不获得 UVM。

journal v3 保存设备配置及预算；v1/v2 继续表示原无设备任务。恢复核对保存的原实例、命令、镜像、安装、锁、设备映射与权限后停止或移除，不执行旧镜像，不重新套用当前驱动，也不直接释放数据库预约。设备映射或身份变化时保留记录并拒绝发送停止信号。

- **504 项组件检查通过，无跳过**：设备选择、映射与能力拒绝、旧记录恢复、观察来源、控制端及编译/驱动/发布兼容。记录为 `test-results/g34-components-final.log` 和 `.xml`。此前 502 项、124 项及 261 项检查为重叠范围，不累加。
- **SQLite 定向检查 111 项通过、2 项跳过**，后两项要求 PostgreSQL 锁；日志为 `g34-recovery-sqlite-fixed.log`。随后实际 PostgreSQL 定向检查 86 项通过、18 项真实 CPU 容器场景因未启用专用入口而跳过，日志为 `g34-postgres.log` 和 `.xml`。不能把跳过项计为通过。
- **真实 CPU 容器与 PostgreSQL 联合回归 195 项通过、零跳过**，包含两种原预算下共十八个实际容器恢复场景，以及 v3 设备记录的数据库替身边界。上述十八项跳过场景在此专用入口实际运行。原报告为 `px-container-database-83726aea2ec6/result.json`，日志为 `g34-database-containers.log`；一次性数据库、卷、容器、原进程与临时存储均由原入口核对清理。定向 PostgreSQL 的 86 项属于本组重叠范围，不累加。
- **七组原无设备控制端回归通过**：UID 与双向锁、错误用户、锁/socket 替换拒绝，以及跨容器、容器至宿主、宿主至容器恢复。使用当前控制源码重建，未启用 NVIDIA 管理查询或映射设备；报告为 `px-container-controller-c1fab4878bc9/result.json`，日志为 `g34-controller-cpu.log`。

数据库替身中的 v3 设备只用于验证持久身份、事务和拒绝边界；它不证明物理设备恢复。

镜像只构建检查通过：`px-g34-build-only-d93eca4ff880/result.json` 核对了 257 个预建目标均为只读空普通文件。检查只从未启动、未配置设备的容器副本读取文件，随后移除该副本和自有镜像标签；没有验证 runsc 的实际设备绑定。整仓 Ruff 与格式检查通过，日志为 `g34-ruff-final.log`、`g34-format-final.log`；十份相关文档的 403 个本地文件链接通过，新增文档后的 36 项发布兼容检查也通过，属于前述组件范围的重叠检查。

## 原始失败与修复状态

首次真实单卡节点检查在正常场景的 guest 启动前失败，报告为 `px-container-gpu-157fea118ddc/result.json`，日志为 `g34-device-acceptance-initial.log`。补充有界运行时诊断后的第二次检查同样失败，报告为 `px-container-gpu-563de0fbe51a/result.json`，日志为 `g34-device-acceptance-diagnostic.log`。两次都没有执行 CUDA 初始化、计算上下文或内核；固定 guest 只设计为读取设备元信息，不能证明它已实际成功运行。

第二次记录明确指出 gofer 无法在只读 guest 根内创建 `/dev/nvidia0` 的绑定目标，错误为 `read-only file system`。修复在镜像内预建空普通文件作为挂载目标，由 OCI 仍然只选择三个真实设备；保持根目录只读，不增加能力或放宽 seccomp。**修复后的真实设备检查尚未重放**，实际 guest 是否只见三个字符设备仍需验证，不能从镜像文件或单元测试推断成功。

重跑被自动审批拒绝：将真实 GPU 设备映射给任务被判定超出了维护者此前“先继续不占 GPU 的开发”的授权。已暂停该检查并请求明确范围；没有改用间接调用绕过拒绝，无设备开发与测试继续进行。设备节点检查即使不运行 CUDA，也必须先取得对应授权。

SQLite 检查最初还有 32 项夹具准备错误：静态清理移除了只用于 pytest 的导入。已使用显式夹具导入修正，保留原始 `g34-recovery-sqlite.log`，随后上述 SQLite 与 PostgreSQL 检查通过；这不是运行平台故障或硬件结果。

## 制品与策略身份

| 身份 | 本次值 |
| --- | --- |
| 单卡任务源码契约 | `284a7709813712a09937a1b3f3a4516e7fa349429b563476fe2ee36d3742d62a` |
| 只构建任务镜像 | `sha256:7fd8b7e990f31e486eeace5d3750c45b27930438f26cf40a3332fe09624df67d` |
| 共享容器运行契约 | `d6f2fa5c80c088608331b98aea9db1bcbc0824fc64a53ca0cdb3d5b803e8d98c` |
| GPU 执行策略 | `ac1f74bd58fac09639121caa164ca7840c4c6b976bae61931ccfbc8f3e6b05e5` |

共享恢复新增依赖已纳入 GPU 执行摘要，因此旧 GPU receipt 不能用于当前代码；没有签发新凭证。CPU/WASI 策略仍为 `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，现有站点未重启或更改数据。原失败报告保留各自执行时的源码身份，不能与修复后的镜像混为一套已通过结果。

最终只读审计确认本批记录的 **38 个容器 ID、8 个镜像标签、19 组原进程**均已清理，包含失败检查、无设备联合回归和未启动的镜像文件检查副本。临时控制目录不存在，已通过回归的源码与报告一致，现有 Web、WASI worker、tutor 均健康。结果为 `g34-audit.json` 与 `g34-audit.log`；没有停止未知对象、重置 GPU 或修改共享服务。

## 后续门槛

单卡 guest 需要重放正常传递、错误 UVM 拒绝、控制器退出和原记录恢复后的新调用，并独立核对清理。之后仍需真实非空 GPU 进程来源、CUDA 正确/错解、资源与故障恢复、完整启动和当前凭证、容器 worker、Compose/发布及正式题型交付。设备允许列表和网页提交保持原关闭条件；不从本次局部结果生成 GPU receipt 或正式成绩。
