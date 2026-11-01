# G63：本地 CUDA 无设备编译与恢复

日期：2026-11-02。基线 `5ba6aff`，实现提交 `de05faa0fb474b778e9fdecae3b429e82f190766`。对应 R-15、R-16 和 [RFC-024](rfcs/024-local-cuda-practice.md)；决定见 [ADR-073](adr/073-local-device-free-cuda-compilation.md)。

## 已交付范围

`practice.py compile` 读取既有 CUDA 公开任务包，在经过验证的无设备 gVisor/NVRTC 任务中编译 `solution.cu`。镜像、状态、Docker socket 与计算架构由操作者指定，任务包不能控制这些配置。结果报告编译类别及源码、版本和编译器身份，不返回 Accepted、正式成绩、原始诊断或 PTX 内容。

新构建入口保留经过固定正确源码/语法错误检查的镜像。私有工作区绑定原目录、实际锁、journal、安装身份和 daemon；中断后由 `recover-compiler` 继续原任务清理。部署选择、构建及恢复集中在[本地编译环境](deployment/local-compiler.md)，日常操作在[本地练习指南](local-practice.md#cuda-compilation)。README 和 AGENTS 已同步。

## 组件与文件检查

| 检查 | 结果 | 原始记录 |
| --- | --- | --- |
| 本地工具及 CUDA 公开包初轮回归 | 94 passed，1 skipped | `test-results/g63-focused-1.xml`、同名前缀 `.log` |
| 状态目录持久化补充后的专项 | 31 passed，无跳过 | `test-results/g63-state-final.xml`、同名前缀 `.log` |
| 最终相关回归：CLI、编译调用、镜像契约、journal、监督器及发布包 | 287 passed，1 skipped，288 项总计 | `test-results/g63-regression.xml`、同名前缀 `.log` |

唯一跳过项是 `test_real_local_public_tests_are_private_and_not_formal`：本轮未提供独立的真实 WASI bundle，不计为通过。本轮未修改 WASI 执行行为。

实际文件检查覆盖私有权限、原目录/锁/安装记录绑定、重复打开与并发拒绝、复制或替换目录、缺失安装记录、硬链接/FIFO/非法标记，以及信号延迟和恢复原处理器。CLI 检查覆盖退出码、公开包基础设施字段拒绝、CPU 包拒绝、私有异常信息不外泄及恢复无需题目/镜像。协议模型不作为真实编译或设备证据。

## 真实无设备构建与 CLI

使用已有私有 rootless Docker 与固定离线归档；未查询 NVIDIA/NVML、初始化 CUDA、映射设备或执行 GPU 工作负载。镜像中的实际身份握手要求无 GPU 节点、无 CUDA 驱动，NVRTC 为 13.1。

| 身份 | 值 |
| --- | --- |
| 编译镜像 | `sha256:1926566b7ec822dc0b044ed26c3755bf6e6bc43c63b1d43e7b29c96d06799694` |
| 编译源码契约 | `c6ff6f743c9934c7bd737e45ae3ac2b9a3a9223c54e4de30e5da09508bb451f5` |
| 固定任务预算 | `container-workload-v1`；0.5 CPU、512 MiB、零 swap、256 PID |

构建入口的正确源码与语法错误两项真实检查通过，原结果为 `test-results/g63-compiler-build-1/build-result.json`；构建日志及调用日志保留在对应目录和 `test-results/g63-compiler-build-1.log`。构建检查时使用的工作区模块早于父目录 fsync 补充；镜像源码契约未受影响。最终 CLI 矩阵使用提交 `de05faa` 中的完整模块字节。

CLI 矩阵首轮 **22 项检查通过**，报告为 `test-results/g63-cli-1/result.json`，调用日志为 `test-results/g63-cli-1.log`；每条命令的 stdout/stderr 另行保留。覆盖：

- 向量加法、矩阵乘法的两份参考实现和六份语义错解均生成 PTX；语义错解可以编译，不能据此认定正确。
- 语法错误退出 1，未知架构作为平台错误退出 2；错误镜像、并发调用和混入 socket 字段的任务包拒绝。
- 在观察原任务实际运行后，对自有 CLI 分别发送 SIGINT、SIGKILL。SIGINT 沿正常路径清理；SIGKILL 保留原活动记录，新编译被拒绝，显式恢复仅移除原任务。原容器进程退出、容器删除及新的正确编译均通过。
- 22 项计数包含中断后的显式恢复、最终幂等恢复和新的编译；不是 22 次不同的 CUDA 执行。

CLI 的 14 个任务及构建检查的 2 个任务均已移除，无活动 journal。`test-results/g63-resources.json` 记录完整容器 ID 和最终只读核对；构建镜像按入口约定保留。原 Web、worker、tutor 三个容器 ID、镜像及启动时间与本轮前及 G62 相同，没有重启、重建或数据导入。

## 发布与验证边界

临时制作包含 927 个文件的真实源码归档，校验新 CLI 模块、构建器、验收器及集中指南字节均进入归档；临时目录已清理。身份和文件清单摘要见 `test-results/g63-source-package.json`。既有发布包与镜像未改写，发布兼容检查沿原有测试完成，没有借此宣称旧 F 应用整站升级通过。

与基线比较，CPU 策略、GPU 执行、控制器、worker 和编译器五类有序源码契约均未变化；文档链接和差异检查通过，记录为 `test-results/g63-identity-docs.json`。新增工具通过既有 `scripts/` 源码范围交付，没有改变数据库结构、Web 镜像的 Docker 权限或 GPU 准入。

**仍未完成**：本地 CUDA 公开样例的实际 GPU 执行、数值比较与性能检查；当前完整 GPU worker 启动、故障恢复和部署验收；旧 F 应用整站升级及托管 CI 失败复验。此次没有开放 GPU 提交、发布候选内容或签发 GPU receipt，完整 PRD 目标继续保留。
