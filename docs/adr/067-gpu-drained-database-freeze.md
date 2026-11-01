# ADR-067：在原 GPU 排空保护下冻结数据库

日期：2026-11-02。状态：采用，内部实现；真实 PostgreSQL 和完整 GPU 发布待验收。对应 [RFC-022](../rfcs/022-gpu-release-coordination.md)、PRD R-09/16。

## 问题

GPU 配置检查必须持设备表、设备行和队列行锁；CPU 冻结则要求目标库没有 owner 会话。直接将两者嵌套会拒绝本次检查连接，先释放检查锁又会留下排空与撤销连接权限之间的空隙。冻结前还须证明原 GPU worker 已退出，不能只停止 Web 和 CPU worker。

## 决定

- 协调器统一通过 `quiesce` 停止并冻结原代；GPU 分支要求实际维护路由、原操作暂停、当前私有配置及完整发布代（包括 Web/CPU worker 名称）。公共 GPU 发布门槛继续保留。
- 先短暂确认全队列和所有物理预约为空，再沿原生命周期停止 GPU worker。等待停止时不持设备或数据库行锁；随后取得原设备锁，重新检查原容器、启动实例、所有登记对象及 journal，才停止 Web/CPU worker。
- `freeze_gpu` 持排空事务直到 runtime CONNECT 撤销、已有 runtime 会话终止及冻结检查完成。检查期间只允许本事务精确 PID 和 owner 的连接；其他 owner、未知角色、prepared transaction 或残余预约均拒绝。
- 持锁事务关闭后必须再次确认目标库无会话。协调器的最终身份检查也结束后，再核对冻结状态；设备锁一直保留到此处。
- 使用既有 worker 停止意图和数据库 CONNECT 的持久状态。每次重试重新核对全部前提，不缓存“已排空”凭据，不自动解冻、清理任务、释放预约或重建原 worker。

## 结果与边界

CPU 发布/恢复仍使用原冻结行为。GPU 分支可供完整协调流程衔接，但尚不等于完整 apply、候选失败回滚或曝光后恢复已经完成。真实文件锁与 CPU 进程中断、SQL/Docker 模型和实际 PostgreSQL 专项分别记录于 [G51](../acceptance-g51.md)；跳过的数据库检查和未运行的 GPU 不算通过。操作与检查入口集中在[部署指南](../deployment/container-gpu-worker.md#database-freeze)。
