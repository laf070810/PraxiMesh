# G64：GPU 公开任务协议与真实 CPU 调用链

日期：2026-11-02。基线 `10b3ab3`，实现提交 `b5cbb6c`。对应 R-15、R-16、[RFC-024](rfcs/024-local-cuda-practice.md) 和 [ADR-074](adr/074-public-only-gpu-task-protocol.md)。

## 实现范围

修复本地 GPU 执行的一个前置缺口：公开下载包没有受保护测试，原 `execute` 契约拒绝这种包。新增独立 `execute-public` 请求和容器适配器方法，只接受公开测试并固定 Run；正式任务仍要求完整测试集，没有补造受保护样例或放宽 Submit。

公开任务与正式任务共用可信数值比较，预期答案不进入 guest。响应必须带布尔 `formal_result: false`，不带性能分数，不能与普通任务完成响应混用。执行继续经过原环境和开始回调、资源限制、任务 guest 清理、独立正确恢复、外层容器删除和空闲观察。

这次交付的是执行协议与适配器接口。**完整本地 GPU 命令尚未交付**：原物理锁、持久任务身份、完整二十四阶段启动、取消及真实硬件验收仍须接入。现有 `compile` 只编译，`test` 仍仅运行 WASI；产品 GPU 提交保持关闭。

## 失败复现与组件回归

新增测试先用真实公开任务结构确认原任务的 Run/Submit 都拒绝缺少受保护测试的输入，再因缺失 `public_task_request` 入口失败。原始记录为 `test-results/g64-public-before.xml`、同名前缀 `.log`，共 1 failed；保留失败而未改弱原断言。

| 范围 | 结果 | 原始记录 |
| --- | --- | --- |
| 首轮协议、比较和容器执行组件 | 168 passed | `test-results/g64-public-after-1.xml`、同名前缀 `.log` |
| 完整相关路径，包含原适配器、公开包和本地编译回归 | 289 passed | `test-results/g64-public-after-2.xml`、同名前缀 `.log` |
| 历史成绩、凭证、完整启动、worker、编译和发布兼容 | 339 passed | `test-results/g64-compatibility.xml`、同名前缀 `.log` |

最终两组共 **628 passed，无跳过**；168 项是其中的先行子集，不重复累计。覆盖公开与正式模式隔离、私有字段和错误预期拒绝、可信比较、响应混用、非布尔标志、性能字段、错误计数、恢复失败后扣留结果，以及环境与旧成绩/发布格式兼容。GPU 会话、环境和设备在这些组件测试中是模型，不能当作硬件证据。

## 真实 CPU/runsc 矩阵

按[集中调用链入口](deployment/gpu.md#container-kernel-flow)运行完整矩阵，**27 个场景首轮通过**。报告为 `test-results/px-container-kernel-3b9b10638950/result.json`，启动日志为 `test-results/g64-kernel-1.log`。

新增九个公开任务场景覆盖正确结果、错误结果、语法错误、运行错误、独立恢复失败、取消、受保护测试混入、Submit 模式混入及后续新调用。混入非法字段/模式时没有创建任务 guest；取消与恢复失败没有返回成功完成响应。每个场景都验证原任务容器移除与实际进程退出。

完整矩阵同时重放原 Run/Submit、维护请求、命名检查、异常恢复以及九项资源检查。CPU、内存、PID 与 cgroup 结果来自实际容器；固定 CPU guest 只把传入源码作为不透明数据，不编译或执行学员代码。这里没有加载 CUDA、查询 NVIDIA/NVML 或映射 GPU，不能证明 CUDA 正确性、设备恢复或 GPU 性能。

| 身份 | 值 |
| --- | --- |
| CPU 协议镜像 | `sha256:cc67391eb9af6d86b0dd13c80b8ecd9c1204ba1961f72472b5610cadc579260b` |
| 运行时源码契约 | `1808f8435dd44e63ed211882cdab743ae85638920e36f0703ca4e81db757b816` |

原始构建曾等待固定基础镜像元数据，原进程随后继续并完成；没有因观察等待而重复启动。27 个原任务均清理，临时状态目录和验收镜像标签已移除，场景日志与 journal 历史保留在报告目录。

## 契约、编译工具与发布

新增方法落在既有有序源码范围内。CPU/WASI 策略未变化；GPU、控制器、worker、运行时、任务镜像和编译镜像契约发生变化。逐文件前后摘要见 `test-results/g64-identity-docs.json`。没有复用旧 GPU receipt，没有修改数据库结构或现有服务。

实际调用确认 G63 旧编译镜像在创建任务前被拒绝，退出码 2，无活动记录和已完成任务记录；证据为 `test-results/g64-old-image.json` 及对应 stdout/stderr。

随后按[本地编译构建入口](deployment/local-compiler.md)串行构建新镜像并通过正确源码/语法错误两项真实 NVRTC 检查。镜像为 `sha256:7b9da5553d0e89220e1f0b8a480913fbe20e945186ad75496228dfed7c494478`，编译契约为 `32bd1fab556b7f71e3c6533cf9e8da1445689303a4766b400cb93c2c8c06aebb`；记录在 `test-results/g64-compiler-build-1/build-result.json` 和同目录构建日志。

实际 CLI 再沿用 G63 自有的合成练习和原状态目录，换用新镜像编译成功。原工作区标记和 14 条历史记录字节不变，只追加本次完成记录，原报告未改写。证据为 `test-results/g64-upgraded-cli.json` 及对应 stdout/stderr。此次编译仍没有执行 PTX 或访问 GPU。

最终只读核对本轮 **30 个任务已移除**：27 个 CPU 协议任务、2 个构建检查和 1 个升级后 CLI 编译；无活动 journal。编译镜像按构建入口约定保留。既有 Web、worker、tutor 三个服务的容器、镜像和启动时间均未改变，完整记录为 `test-results/g64-resources.json`。

临时制作并校验 930 文件源码归档，新协议涉及的五个模块逐字节一致；记录为 `test-results/g64-source-package.json`，临时目录已清理。此检查只证明这些模块随源码交付，不是新应用镜像或 GPU 部署验收，也没有改写任何旧冻结包。

完整本地 GPU 工具、当前硬件执行/性能/故障恢复、GPU 部署、旧 F 应用整站升级和托管 CI 仍未完成。公开运营与许可证继续等待维护者决定；完整 PRD 目标保持不变。
