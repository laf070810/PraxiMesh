# G52：GPU 恢复后的独立候选验证轮次

日期：2026-11-02。对应 PRD R-09/16；[决策](adr/068-gpu-candidate-verification-rounds.md)、[设计](rfcs/022-gpu-release-coordination.md)。没有查询或使用 GPU、调用真实 Docker、启动 PostgreSQL 或改动既有站点服务。

同一操作中的原 GPU worker 重启后，可在前轮已验证/清理后创建新验证轮次。轮次 UUID 在命令执行前落盘，进入精确测试账号、题目与提交身份；同一启动继续原轮次和期限。原暂停 token、启动实例、环境和 owner 校验保留，旧轮成绩不授权新启动。

协议为 `gpu-deployment-check-v2`，管理命令增加必需的 `--round-id`，协调日志使用 schema 2。清理继续识别原轮次，并可采用恢复后的新鲜 owner；不会误删另一轮记录。未知轮次、未完成前轮、同一启动的重复轮次与超过 16 轮上限均拒绝，完整历史保留。旧冻结包只读校验仍兼容，新命令需要匹配制品，不改写旧私有记录。

`test-results/g52-round-final.xml/.log`：**177 passed、2 skipped**。检查实际使用一次性 SQLite、私有文件、原文件锁和 CPU 协调器子进程；Docker、设备环境、成绩和管理命令为显式模型。覆盖原轮次重试、轮次隔离清理、owner 改变但未重启的拒绝、未知轮次、重复启动标识、轮次上限，以及既有配置发布、恢复和冻结守卫。两项实际 PostgreSQL 专项因 SQLite 环境跳过，不计为通过。

`g52-round-before.xml/.log` 保留两项原始失败：原检查中断后完成恢复/清理，以及原检查通过后 worker 重启，两者都无法再验证新启动。`g52-round-initial` 的 120 passed、2 skipped 是与最终结果重叠的中间记录，不累计。一次文档补丁因原句差异未写入；核对工作区后修正匹配位置。

本轮修改应用候选命令，GPU worker 源码契约随之变化；CPU/GPU 执行策略及控制器未改。完整候选准备、曝光前回退、曝光后原库恢复仍需连接和真实验收，公共 GPU 激活保持关闭。此增量使恢复后的验证可以继续，不等于 GPU 评测或部署已完成。
