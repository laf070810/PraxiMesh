# G53：GPU 候选准备与原库恢复串接

日期：2026-11-02。对应 PRD R-09/16；[设计](rfcs/022-gpu-release-coordination.md)、[决策](adr/069-recoverable-gpu-candidate-preparation.md)、[集中入口](deployment/container-gpu-worker.md#candidate-preparation)。本轮没有实际 Docker、PostgreSQL 或 GPU 操作，没有更改既有站点服务。

## 已连接的流程

内部候选准备现在连续调用原配置发布、Web/CPU 服务、固定预检、GPU worker 创建/启动、冻结环境就绪、CPU 检查及 GPU 轮次验证。私有日志保留完整发布代、原操作、规范归档路径和每次准备尝试；重试复用原对象，已完成阶段仍检查当前就绪及验证轮次。

候选恢复先在原数据库启动 CPU worker 以处理遗留 CPU 工作，然后执行既有 GPU 原库恢复；只在原恢复记录和新鲜实例一致后清理旧轮。新的准备尝试保留旧记录，重建无状态 Web，并验证 CPU 和新启动对应的 GPU 轮次。失败清理不创建新轮，恢复确认丢失也不重复启动 GPU worker。

流程保持维护路由和准入暂停，不更换数据库或代替整站曝光决策。公共 GPU 激活继续拒绝；完整 apply、曝光前失败回退、曝光后原库恢复及 GPU→CPU 仍须继续连接和真实验收。

## 验证范围

`test-results/g53-components-final.xml/.log`：**646 passed、13 skipped**。覆盖候选准备、验证轮次、配置发布、生命周期、原库恢复、冻结、原 CPU 发布、Web 恢复及制品校验。13 项实际 PostgreSQL 专项因 SQLite 环境跳过：6 项配置/就绪锁、5 项数据库冻结、2 项候选队列事务；不计为通过。

组合子集 `g53-preparation-final.xml/.log` 的 118 项检查通过，与最终结果不累计。其中包含 13 处实际协调器 CPU 子进程 SIGKILL：十个首次准备阶段，以及恢复 ready、旧轮清理完成、新尝试准备服务三个位置。父进程确认信号并回收自有进程；重复执行保留原容器和原轮次。

实际执行的是私有文件、文件锁、源码归档校验和 CPU 进程中断；Docker、代理、数据库、设备环境与队列成绩为显式模型。测试包括无预创建容器的合成初始状态、预检失败、CPU 检查失败、GPU 检查失败、清理失败、移动归档、错误服务名/路由/暂停、就绪失效和损坏日志。没有用这些结果签发 receipt 或宣称 CUDA、物理恢复和整站部署成功。

`g53-round-binding-before.xml/.log` 保留两项真实发现的集成缺口：已验证轮次的设备 UUID 或运行契约摘要改变时仍可能被准备阶段接受。现已与原绑定设备和当前生命周期逐项核对；响应比较也保持规范 JSON 的严格字段/类型。验证确认不匹配时继续原阶段，重试不会再次提交已验证的 GPU 任务。

`g53-cpu-flow` 为 52 passed，`g53-preparation-initial` 为 19 passed，均为中间结果。首次 Ruff 发现新模块导入顺序，已经修正；测试补丁曾因格式化换行匹配失败，核对未写入后修正位置。

Ruff、格式检查、未设置 Django 环境时的模块导入及九份文档的 463 个本地链接通过。`g53-identity-docs.json` 记录最终源码摘要：本次仅增加宿主协调代码，CPU/GPU 执行策略、控制器和 worker 契约均与 `afb2826` 一致；G52 的 v2 worker 制品要求继续有效。README 的当前能力和启动命令已核对，无需改变。
