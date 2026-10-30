# G10：启动接管与维护恢复

日期：2026-10-31。设计见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#受信运行适配器)和 [ADR-026](adr/026-gpu-startup-recovery.md)。继续按维护者要求不占 GPU；本轮验证进程、文件和数据库恢复边界，没有执行 CUDA。

## 实现与状态

`core/gpu_startup.py` 将持锁接管、隔离停用、旧任务及维护服务停止、映射存储回收和固定正确解维护连接起来。每卡固定的维护身份支持没有学员任务时的异常接管；未知服务和目录不在清理范围内。所有旧服务停止前不处理目录或开始新维护，任意停止失败都保留现场。

适配器增加只含 `recover` 的独立维护协议，控制器复用新容器中的固定正确解恢复；维护消息不含学员输入，回复不能包含成绩。新所有者、原预约和总期限在恢复期间持续核对。启动阶段最多 180 秒，单次维护最多 90 秒，未作为已验收的 GPU 产品时限。

恢复成功仍返回 `ready: false`，保留隔离停用与旧预约，不消费或重跑旧学员任务，不签发 receipt。正式启动就绪检查、凭证与 worker 仍待完成。

## 检查结果

- `test-results/g10-control-final.xml`：**113 passed，无 skipped**。覆盖维护与任务分离、维护只发送固定消息、错误/成绩/数字冒充布尔回复拒绝，以及已有执行、服务和文件回收边界。通道与 GPU 运行时使用替身，不证明物理恢复。最初未加入扩展边界时的 106 项结果保留在 `g10-control-initial.xml`。
- `test-results/g10-startup-postgres-final.xml`：**54 passed，无 skipped**。真实临时 PostgreSQL 与真实文件锁覆盖新所有者接管、旧预约及账目保留、无任务的固定维护身份、失败停止、所有权/预约变化、账号删除和禁止重新领取。既有执行结算、租约、锁等待及权限检查同时通过；GPU 维护使用替身。自有数据库容器和卷已清理，先前 53 项记录保留在 `g10-startup-postgres.xml`。
- `test-results/px-gpu-recovery-d07f8e292ba4/result.json`：**四项真实 CPU/runsc 检查通过**。任务控制器退出、整服务终止、独立硬期限及新增的维护服务中 CPU 夹具退出，都在 guest 确实运行并创建宿主不可读的映射目录后触发。确认原进程树终止后，独立回收服务恢复同一映射并删除自有存储；活动任务拒绝回收和回收服务中断后的接管也通过。维护场景验证服务身份和文件回收，没有调用生产控制器的 CUDA 恢复分支。
- `test-results/px-gpu-controller-fcf4d737e1c1/result.json`：真实私有命名空间内的新控制器成功加载、完成对端身份核对，并在收到错误顺序请求后返回失败、关闭通道和退出。没有提供 GPU 目标，也没有打开运行时归档。

`test-results/g10-cleanup-final.json` 复核上述两份实际运行报告的源码摘要与当前工作树一致，九个自有服务均已卸载、无 cgroup，两个临时根目录已删除。没有删除未知资源。重放命令统一维护在 [GPU 部署指南](deployment/gpu.md#不占-gpu-的异常恢复检查)。

## 策略身份与范围

GPU 执行摘要为 `65a9ed1a1ed8ef61697144a380334beb22393e02e4717329e07071d2fd55a286`，包含新增的启动协调源码。CPU 策略摘要变为 `ae5cfea16ee6c6fa5cc76b5a8356fc3e30c27bfe8a86de27c8456ca55c2111c8`：`core/gpu_resources.py` 也在共同校验范围内。最终核对最初错误地假设 CPU 摘要不变，失败及原因保留在清理报告；旧 receipt 不能用于新源码。

补验最初使用历史 `var/wasi-functions-b2`，在 `agent/correct` 处超时，没有生成新 receipt；日志保留在 `test-results/g10-wasi-probes.log`。该 bundle 不含标准库字节码。使用同一固定公开 Agent 正确解定向复现时，第一轮 CPU 约 1.009 秒后超时；当前 Compose bundle 的六轮全部通过，总 CPU 约 1.437 秒，详见 `test-results/g10-wasi-agent-diagnosis.json`。两者运行文件只有标准库 zip 不同，比较记录在 `g10-wasi-bundle-comparison.json`，未改动题目资源预算。当前 bundle 来自已验收的 `px-compose-f5b1318868df/wasi`，清单摘要与其实际运行记录一致：`ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`。

使用当前 bundle 与本轮源码重新执行完整 **129 个真实 WASI 探针，全部通过**，日志为 `test-results/g10-wasi-probes-final.log`。`g10-wasi-receipt.json` 由真实检查入口生成，绑定上述新 CPU 策略、当前 bundle 和完整检查集；没有沿用旧凭证。标准库缓存的历史构建差异及重建入口补充到 [WASI 指南](deployment/wasi.md)。这证明当前宿主的 CPU/WASI 组件通过，不等于重建或重新验收 Compose 部署。

没有变更数据库 schema、现有题库或页面；本轮没有重跑 Compose、浏览器、完整 G7 CPU guest 矩阵或任何 GPU 硬件场景。

Ruff、格式及差异检查通过，本次文档的本地链接目标已核对。

## 未完成

正式 GPU 就绪检查、启动 receipt、worker 命令及健康准入、内容与学习证据、可比性能榜单、硬件正确性/故障/恢复、完整时限、实际队列和部署仍待完成。G1/G2 的历史硬件结果不覆盖本轮代码，数据库替身与 CPU 文件回收成功也不能开放 GPU 提交。
