# G24 持久容器身份与升级清理

日期：2026-10-31。实现前基线为 `main` 的 `a0308c7`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)和 [ADR-040](adr/040-durable-container-ownership.md)，命令统一在 [GPU 指南](deployment/gpu.md#container-journal)。本批不占 GPU，不修改共享 daemon，不开放正式提交。

新增私有安装身份、逐设备活动记录与归档。写入发生在创建、启动等操作之前或紧接确认之后；恢复按保存的 v1 契约停止旧容器，不按当前源码去运行旧制品。持久名称与当前进程 UID 解耦，同时保留私有文件、同一锁目录和 daemon 核对。

## 实际结果

修复存储兼容性后，`test-results/px-container-journal-6624ddc51a4a/result.json` 的七项检查及总 `passed` 均为 `true`；每项随后都通过新的正式控制连接运行固定 CPU guest，并验证内部清理及整容器进程消失。

| 中断或故障 | 实际观察 |
| --- | --- |
| 意图写入后 | 控制器以 17 退出，尚无容器；新进程归档未执行的意图，允许新调用 |
| 创建后、ID 未记录 | 实际容器处于 created，记录尚无 ID；按原名称、nonce、镜像及完整配置找回，保存 ID 后清理 |
| ID 已记录、尚未启动 | 实际容器处于 created；新进程按完整 ID 清理，不运行旧入口 |
| CPU guest 运行中 | 原创建者以 17 退出时 guest 仍在忙等待；新进程清理后记录的宿主进程身份全部消失 |
| 源码契约变化 | 新控制器拒绝将旧镜像用于当前执行；按持久 v1 契约停止并归档旧对象，恢复过程未调用 create/start/exec |
| 同名替换 | 原 ID 被删除后创建测试自有替换容器；恢复拒绝操作，替换容器仍运行、原活动记录不变；测试控制器单独清理替换对象后才继续恢复 |
| 归档后、活动记录未移除 | 原容器已消失、归档已完整写入，控制器以 17 退出；新进程验证相同归档并完成剩余步骤 |

升级检查从隔离源码副本运行，追加固定注释得到不同源码摘要，实际使用相同 v1 恢复语义；这证明旧执行契约拒绝与清理兼容，不代替新语义版本或生产站点升级。原契约为 `3e6d2d3821bcc12aa33bf64581ef160ca5a7128fc3dedc76392dc5bc2f047549`，副本契约为 `5e6d71b66988ce658a95ab6aafb7cfa647a741602eb930c71c3e3c0f39dfeebc`。镜像为 `sha256:082fae2a13ee3db7a41502a06b7e86923e96d267af5c8fab4fe9a68897a0855a`，沿用固定 Python 镜像及完整 runsc 归档。

本组共确认 14 个自有外层容器移除，镜像标签已删除；四次中断前运行真实 guest，七次新调用验证独立恢复。每项保留原活动记录、归档、实际 PID/启动身份和源文件摘要。目录中的合成锁及审计记录保留，不作为生产凭证。

初轮 138 项组件检查通过，存储修复专项 142 项通过，最终相关组件与发布兼容检查 206 项通过、无跳过，见 `test-results/g24-components-{initial,storage,final}.log` 及 XML。Ruff、538 个 Python 文件格式检查、无待生成迁移和既有 50 节点/26 题静态检查通过；静态检查没有执行参考代码，记录见 `test-results/g24-{ruff,format,migrations,content}.log`。

原监督器的七类真实回归通过，八个测试容器移除，见 `test-results/px-container-supervision-229fb5f0e3c0/result.json`。同一运行时镜像上的九次原通信/恢复调用也通过，九个测试容器移除，见 `test-results/px-container-transport-1827e4c9a880/result.json`。两组镜像标签均已移除。

最终复核确认三组成功报告的源码摘要与当前文件一致，合计 31 个测试容器均已不存在，原有三个站点服务仍健康；CPU/GPU 正式执行策略摘要未变。143 份 Markdown 的 791 个本地链接、改动文档的 37 个锚点及新增 CLI 参数核对通过。记录：`test-results/g24-audit.json`、`test-results/g24-document-links.json`。

## 原始失败与修复

首轮 `test-results/px-container-journal-62320f56ad42/result.json` 在创建任何任务容器之前失败：本机项目目录是 NFS，`renameat2(RENAME_NOREPLACE)` 返回 EINVAL；pytest 的临时目录位于 XFS，因此初轮组件检查没有暴露该差异。清理阶段再次初始化记录也遇到相同错误；当时没有任务容器，构建的镜像标签已移除。原日志保留在 `test-results/g24-journal-initial.log`。

修正为持有同一私有元数据锁，在允许创建检查后执行普通原子重命名，仍同步文件与目录。同一对象的线程也串行化，安装身份或元数据锁被更换时拒绝继续。实际 NFS 上 12 次并发发布只有一次成功，没有临时文件残留，记录均为 0600；见 `test-results/g24-nfs-publication.json`。该组合继续保持原子发布，没有以复制或截断原记录降低要求。

## 范围

验收使用合成设备/任务身份和固定 CPU guest，不建立应用数据库。跨 UID 的 worker 容器映射、数据库预约与就绪事务、设备注入、生产资源契约、CUDA 正确性/性能及完整 Compose/发布升级仍需继续完成；本批不能作为 GPU 就绪证明。这里验证的是进程中断，不包含主机断电或存储故障切换。G22 的完整资源矩阵、PostgreSQL/WASI 全链路、Compose 与浏览器未重放，原站点未重建。
