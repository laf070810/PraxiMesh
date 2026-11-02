# G65：独立容器启动流程与无设备部署复验

日期：2026-11-02。实现基于 `d551ec4`，启动流程提交为 `a4cf61f`。遵循维护者“先继续不占 GPU 的开发”；本轮没有查询 NVIDIA/NVML、初始化 CUDA 驱动、映射设备或执行 GPU 工作负载。

## 实现与范围

`judge/container_startup_suite.py` 将完整容器启动检查整理为不依赖 Django 或应用数据库的共用流程。它校验可信调用者刚完成的初始恢复报告，随后依次执行资源、十九项内核和三项控制器故障检查，保持原期限、原任务清理、报告校验及所有权/取消回调。只有完整二十四阶段通过才返回证据。

服务端 `core/gpu_readiness.py` 调用同一流程，继续管理实际初始恢复、旧预约、凭证撤销/发布和最终数据库行锁。共用函数不发布 ready，也没有接受外部报告来跳过检查的命令。设计见 [RFC-024](rfcs/024-local-cuda-practice.md)与 [ADR-075](adr/075-shared-container-startup-suite.md)。

这为完整本地 GPU 公开样例执行提供了共用启动流程，但本地命令、共享物理锁和持久本地生命周期尚未接入。现有 `compile` 仍只编译，`test` 仍仅运行 WASI。

## 组件与实际数据库

| 范围 | 结果 | 原始记录 |
| --- | --- | --- |
| 既有容器/宿主启动与私有凭证 | 81 passed | `test-results/g65-readiness-initial.xml`、同名前缀 `.log` |
| 独立流程、失败、取消和无 Django 导入 | 33 passed | `test-results/g65-standalone-2.xml`、同名前缀 `.log` |
| worker、发布包及本地编译兼容 | 135 passed / 12 skipped | `test-results/g65-compatibility-1.xml`、同名前缀 `.log` |
| 真实 PostgreSQL 原启动及发布/心跳行锁竞争 | 37 passed | `test-results/g65-postgres-1.xml`、同名前缀 `.log` |
| 补跑上述 12 项 worker 数据库权限检查 | 12 passed | `test-results/g65-postgres-worker-1.xml`、同名前缀 `.log` |

独立流程测试使用实际私有锁和 journal 文件，GPU 方法使用明确的模型；未授予测试应用数据库访问。覆盖全部后续阶段失败、进度回调取消、错误恢复范围/环境/清理、最后阶段的拥有者/环境/期限变化，以及独立子进程不加载 Django 或 core。首轮因测试导入路径写错而收集失败，保留在 `g65-standalone-1.xml`、同名前缀 `.log`；修正测试导入后重放通过。

实际 PostgreSQL 共 49 项检查通过，两个一次性数据库容器和卷由各自入口清理。37 项中包含与 SQLite 重复的服务端回归；按用例身份去重，以上共 **265 项通过**，统计见 `test-results/g65-test-summary.json`。SQLite 跳过的 12 项已全部在 PostgreSQL 执行，没有尚未补跑的跳过项，不能把原跳过记录本身当作通过。设备和执行报告仍为模型，不能证明 GPU 启动成功。

## 实际无设备 worker 镜像

按[集中 worker 验收入口](deployment/container-gpu-worker.md#开发时不占-gpu-的验证)，从当前源码构建实际镜像，并使用独立 Compose 项目与 PostgreSQL 角色运行 **9 项检查，全部通过**。包含最小权限、重建、显式与默认关闭、公开配置拒绝、额外列授权拒绝、源码篡改拒绝、恢复有效配置及旧无设备控制器早期拒绝。

报告为 `test-results/px-container-worker-c4f82d45bbba/result.json`，启动日志为 `test-results/g65-worker-1.log`。`gpu_queried`、`gpu_mapped`、`gpu_executed` 均为 false；未写 GPU 设备行、提交或运行凭证。测试只静态复制经过摘要检查的管理库与查询程序，没有加载或执行它们。固定 CLI 归档经过仓库内置 SHA-256 校验。

| 身份 | 值 |
| --- | --- |
| worker 镜像 | `sha256:3fe439e3e835f753448642f13fe9d00b7108638af9727abcf9909da249f17684` |
| worker 源码契约 | `86c3ad9b93f0a8cf493c3c23d522da8b2782642dac5ff9936b65ef37932d1cdc` |
| 控制器源码契约 | `dd1f5d1f86c479fda26505043288f8bce5f583cf710259a1e690c66be523a3c9` |

原构建等待固定基础镜像元数据后继续完成，没有因观察等待重复启动。该入口已清理自有容器、网络、卷、临时状态及验收镜像标签；报告中的 `owned_resources_removed` 为 true。

## 默认 Compose 完整回归

新增策略文件还必须随 Web 与 CPU worker 一并交付。按[默认 Compose 验收入口](deployment/compose.md)创建独立项目，首次构建启动与保留数据卷重建均通过；报告为 `test-results/px-compose-8917177e8d88/result.json`，启动日志为 `test-results/g65-compose-1.log`。

- 镜像内实际执行 129 项 WASI 隔离探针；CPU 策略为 `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9`。
- 七种题型各一份正确 Submit、错误答案及公开 Run 共九个真实任务得到预期结果；worker 离线时关闭提交，页面仍可访问。
- 独立临时端口的 `0.0.0.0` 与直接 IP 访问、同源草稿 POST 和跨源拒绝通过。账号、草稿、会话、密钥、自有内容/撤回和九份成绩在重建后保留。
- 初始与重建后的 Web/CPU worker 都计算出同一 GPU 策略摘要 `44bc387fec88bd98685a4283a7f80c9f99f07c0fcd9014be73613a5360316dbc`，与当前源码一致；Web 没有获得 Docker socket。

本轮重放默认配置，没有新增界面或模型变化，未重跑可选 AI 与浏览器矩阵。其既有验证范围分别保留在对应历史报告，不能算作本轮结果。

## 编译工具、源码与清理

新模块纳入有序 GPU 执行契约；CPU/WASI 策略保持不变。GPU、控制器与 worker 契约各涉及三个变动文件；编译器、运行时和任务镜像均因共同的 `judge/gpu_policy.py` 发生契约变化。逐文件记录为 `test-results/g65-identity-docs.json`，不能沿用不匹配的旧镜像或运行凭证。

按[本地编译构建入口](deployment/local-compiler.md)重建匹配镜像，并通过正确源码、语法错误两项实际 NVRTC 检查。结果为 `test-results/g65-compiler-build-1/build-result.json`，同目录保留原构建日志与私有 `check-state`。镜像为 `sha256:9926ca7d2c1d29cb9629241fad575b1ff42b4e8b8d33411d7cceaadb50d79a88`，编译契约为 `bfb62f4787207bd501f7e7caeb9af0725b341759a87e653314d61bcfa6ff4ce6`。该镜像按入口约定保留，编译成功不表示公开样例正确或 GPU 已就绪。

只读验证原 `var/releases/2026-11-02-g59-cpu-2` 冻结 CPU 制品通过，记录为 `test-results/g65-frozen-cpu.json`；没有导入旧镜像、执行归档代码或改写发布包。临时制作并核验 935 文件源码归档，新模块及两个修改模块逐字节一致；记录为 `test-results/g65-source-package.json`，临时目录已清理。这只证明源码交付，不替代新的生产发布包或 GPU 部署验收。

最终只读核对 worker 预检的十个容器和两次编译任务容器均已移除，编译 journal 没有活动记录；默认 Compose 自有容器、网络和卷也已清理。编译镜像按构建入口约定保留。既有 Web、worker、tutor 三个服务的容器 ID、镜像与启动时间均未改变，记录为 `test-results/g65-resources.json`。

完整本地 GPU 工具、当前硬件执行/性能/故障恢复、GPU 部署、旧 F 应用整站升级和托管 CI 仍未完成。公开运营与许可证继续等待维护者决定，完整 PRD 目标保持不变。
