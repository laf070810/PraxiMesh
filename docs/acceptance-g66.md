# G66：本地 GPU 内部生命周期与原状态恢复

日期：2026-11-02。实现基于 `c8490d6`，生命周期提交为 `8c51810`。遵循维护者“先继续不占 GPU 的开发”，未查询 NVIDIA/NVML、初始化 CUDA 驱动、映射设备或执行 GPU 工作负载。

## 实现与交付范围

本地内部执行现在把持久操作、初始正确恢复、完整二十四阶段启动、公开任务、原任务清理及终态保存连成同一流程。状态绑定原 v4 控制器目录、物理 UUID 文件锁、mounts 摘要、安装与 daemon；不从练习包新建另一套设备锁，不接管服务器的数据库预约。worker 配置读取/写入和协调器首次认领拒绝本地状态。

未完成操作阻止新任务。显式恢复只接受原操作的 UUID、冻结镜像、设备选择和固定内核命令；清理后仍返回 `ready: false`，下一次执行重新完整启动。原绑定和历史记录可跨源码/镜像升级保留，旧任务继续按旧记录恢复。

实现见 `judge/container_practice.py`、`judge/container_practice_state.py`，设计见 [ADR-076](adr/076-local-gpu-operation-lifecycle.md)和 [RFC-024](rfcs/024-local-cuda-practice.md)。这是可信控制器内部接口；外部 CLI、固定控制镜像/通信和真实 GPU 验收仍须接入。现有 `compile` 仍只编译，`test` 仍仅运行 WASI。

## 原始失败与组件验证

首轮 45 项检查通过，随后新增的终态持久化后取消检查失败：取消发生在操作已保存、结果尚未返回的窗口，原实现未再次检查。原始记录为 `test-results/g66-final-boundary-before.xml`、同名前缀 `.log`，1 failed。修复在保存终态后，再次核对取消、原操作/控制器/环境和总期限；终态已持久化也不会强行返回被取消的结果。

| 范围 | 结果 | 原始记录 |
| --- | --- | --- |
| 首轮本地生命周期 | 45 passed，后续完整组的子集 | `test-results/g66-local-1.xml`、同名前缀 `.log` |
| 修复后生命周期、worker 配置及协调器互斥 | 139 passed | `test-results/g66-local-2.xml`、同名前缀 `.log` |
| 原启动、worker、公开协议、编译与发布兼容 | 186 passed | `test-results/g66-compatibility-1.xml`、同名前缀 `.log` |
| 真实 PostgreSQL 最小权限及就绪行锁竞争 | 16 passed | `test-results/g66-postgres-1.xml`、同名前缀 `.log` |
| 新镜像下按原冻结身份恢复 | 1 passed | `test-results/g66-upgrade-state-1.xml`、同名前缀 `.log` |

最终四组按用例身份去重共 342 项通过，无跳过，统计为 `test-results/g66-test-summary.json`。覆盖全部启动阶段和公开执行失败、晚期取消/身份/环境/期限变化、终态写入失败、非法公开包、未知服务器状态拒绝、原镜像与命令匹配，以及完整启动每次重跑。PostgreSQL 使用一次性容器/卷并已清理；GPU 方法仍为模型。

三处实际子进程 SIGKILL 分别发生在操作登记、任务意图保存和终态保存之后。子进程持有真实文件锁，父进程确认锁竞争拒绝；退出后重新取得同一锁 inode，保留原绑定和操作 UUID，并按原记录恢复。子进程的 daemon 接口只返回合成 UUID，没有创建 Docker 容器或访问设备，不能将这些结果当作 GPU 故障恢复通过。

## 当前镜像与应用复验

按[集中 worker 入口](deployment/container-gpu-worker.md#开发时不占-gpu-的验证)从本轮源码构建实际镜像，十项无设备 Compose/PostgreSQL 检查通过。在原九项基础上，新增本地状态标记的拒绝检查；该检查在读取 worker 配置阶段失败，不进入数据库检查或 GPU 控制器。测试结束时没有新增设备行、提交或 GPU 凭证。

记录为 `test-results/px-container-worker-5fcf95fee380/result.json`，启动日志为 `test-results/g66-worker-1.log`。`gpu_queried`、`gpu_mapped`、`gpu_executed` 均为 false。构建只静态复制并核对管理库/查询程序，没有加载或执行；自有容器、网络、卷、临时状态和验收镜像标签已清理。

| 身份 | 值 |
| --- | --- |
| worker 镜像 | `sha256:f0677e4f85975a5965af9ea220b239de19aa7f5040b896113fd959aca667a84a` |
| worker 源码契约 | `04f55e3071009e3c01aa9ab814bc58aa4552af6e2af6d37b34bbac2f1e7a4b51` |
| 控制器源码契约 | `7b1997382df29d953e69c05c2c7f334ab33ea0bdfceab3227cd811caebe755bc` |

按[本地编译构建入口](deployment/local-compiler.md)重建配套镜像，正确源码与语法错误两项真实 NVRTC 检查通过，记录为 `test-results/g66-compiler-build-1/build-result.json`。镜像为 `sha256:67e2903eff9c6503692bf5c112b6626c6dc0a79155936a40ff4265d93d174870`，编译契约为 `a4dfd79341aa78b4276f4d3b91f13163ced493d328822cae095162cdddac6421`。编译没有执行 PTX 或访问设备，镜像按构建入口约定保留。

新增模块纳入 GPU/控制器/worker 源码契约；共同的策略文件还使编译器、运行时和任务镜像契约变化，逐文件记录为 `test-results/g66-identity-docs.json`。CPU/WASI 策略未变。只读核验原 `var/releases/2026-11-02-g59-cpu-2` 冻结 CPU 包通过，记录为 `test-results/g66-frozen-cpu.json`，没有导入旧镜像、执行归档代码或改写制品。

## 默认 Compose、源码与清理

按[默认 Compose 入口](deployment/compose.md)以独立项目和临时端口运行完整回归，通过镜像内 129 项真实 WASI 隔离探针、七种题型各一份正确 Submit、错误答案及公开 Run。worker 离线时关闭提交，页面仍可访问；直接 IP/CSRF 检查通过。

保留数据卷重建后，账号、草稿、会话、密钥、自有内容/撤回和九份评测记录仍保留。初始与重建后的 Web/CPU worker 均计算出同一 GPU 策略摘要 `88a539eace3d5655626a6494ea7c4f93dfb63c04bcd497456678e9f500a12164`，与当前源码一致。Web 没有获得 Docker socket。报告为 `test-results/px-compose-d1cda88b1f62/result.json`，启动日志为 `test-results/g66-compose-1.log`；本轮未改界面或模型，没有重跑可选 AI/浏览器矩阵。

临时制作并核验 940 文件源码归档，两个新模块、worker 配置与准备入口、策略文件逐字节一致，记录为 `test-results/g66-source-package.json`。临时目录已清理；这证明源码交付，不替代新的生产发布包或真实 GPU 部署验收。

最终核对 worker 预检的十一个容器和两次编译任务容器均已移除，编译 journal 无活动记录；默认 Compose 自有容器、网络和卷已清理。编译镜像按入口约定保留。既有 Web、worker、tutor 三个服务的容器 ID、镜像和启动时间均未改变，记录为 `test-results/g66-resources.json`。

完整本地 GPU 工具、当前硬件执行/性能/故障恢复、GPU 部署、旧 F 应用整站升级和托管 CI 仍未完成。公开运营与许可证继续等待维护者决定，完整 PRD 目标保持不变。
