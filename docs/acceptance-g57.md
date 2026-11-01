# G57：真实 PostgreSQL 权限、冻结与复制验证

日期：2026-11-02。对应 PRD R-09/16、[RFC-022](rfcs/022-gpu-release-coordination.md)与 [ADR-072](adr/072-coordinated-gpu-release-recovery.md)。重放命令统一在[数据库验证](deployment/production.md#database-validation)。没有执行 GPU、设备查询、WASI 或站点升级。

## 实际检查范围

本轮使用当前用户的原 rootless Docker 和已缓存的固定 PostgreSQL 17 镜像，仅创建自有临时容器与卷。镜像为 `postgres:17-alpine@sha256:e4cb93687b062cd25a0e8f672d85a7708235009d70d31f8a7698d9370092a9c4`，本地 ID 为 `sha256:ab20e934b27ba3ff927c1b4c9ff8cf6c32f315f743efe68dd3441182a87fb085`。没有拉取、恢复或删除其他镜像缓存，也未重建既有 Compose 服务。

新增 `tests/test_deploy_database_postgres.py` 的九项真实数据库检查。runner 为自有容器加验收标签并向测试提供其身份；测试同时核对标签和实际回环端口，使用随机实例名、数据库和 Web/worker 角色，真实 owner/marker 与原 advisory lock 保留。当前迁移在全新代际库运行，未导入内容或启动评测。结束时逐一核对标记后清理自有数据库和角色，外围入口再清理容器和卷。

- `owned_exists` 区分确实缺失、正确所属、无标记、其他站点标记和其他所有者；查询不认领或改写对象。故意损坏的夹具只在原 OID 和精确变更值仍匹配时恢复，再按正常标记清理。
- 当前完整 schema 的真实授予、冻结及重复解冻通过。Web 角色写入合成账号，worker 不能读取账号；冻结后两个 runtime 角色均无法连接，解冻后数据和原权限保留。
- 为 worker 增加账号列读取、为 Web 增加设备列写入，或增加列权限的转授权后，解冻拒绝继续且两种 runtime 连接仍关闭。
- 原冻结库经容器内真实 `pg_dump/pg_restore` 复制，使用原实现逐表比较行、列与全部序列。目标库授权后由 Web 角色写入新值，冻结/解冻后该值保留，原库仍保留原值。此处模拟开放后的数据写入，没有运行 HTTP 代理或完整站点恢复。

原有专项还验证了真实 PostgreSQL 设备/队列行锁、表锁、advisory lock、CONNECT 撤销和 runtime 会话终止；候选准备的临时准入不向外部事务暴露，候选观察等待不持锁。更广的事务矩阵覆盖正式准入额度、最小权限、就绪/环境的原子发布，以及任务领取和结算期间的身份锁。

GPU 状态、执行器和成绩仍使用明确的合成夹具；这些结果不证明 CUDA 正确性、性能、设备物理清理或完整 GPU 发布。后续 [G58](acceptance-g58.md) 已补齐 Web 端口恢复专项，[G59](acceptance-g59.md) 已通过当前 CPU/HTTPS/WASI 的完整发布恢复矩阵；旧版 schema 升级和获授权独占 GPU 上的验收仍须继续。

## 原始结果与修正

最终 `test-results/g57-postgres-verified.xml/.log`：**77 passed、0 skipped**，耗时 84.16 秒。涵盖本页所述完整 schema 检查、配置/冻结/候选事务，以及准入、就绪和 worker 事务；各次子集不累加为总数。

`test-results/g57-postgres-initial.xml/.log` 的 14 项原 PostgreSQL 专项通过，无跳过。新增完整 schema 检查后，`g57-database-initial.xml/.log` 的 23 项通过，无跳过；两次均记录了自有临时数据库容器及卷清理。

扩大到候选、准入、就绪与 worker 事务后的原始 `g57-postgres-final.xml/.log` 为 76 passed、1 failed。失败来自未知状态夹具使用了 13 字符的 `unknown-state`，超过真实 `varchar(12)`，因此还未进入待验证的清理拒绝逻辑。改成仍不属于合法终态的 `unknown` 后重放；没有放宽应用规则、数据库字段或接受结果的条件。原始日志保留，不覆盖。

所有结果使用一次性 PostgreSQL 服务与合成数据，没有把 skipped、SQLite 或模型结果算成实际数据库通过。README 的现有启动说明仍适用，部署操作继续集中维护；本轮没有修改应用模型、迁移、内容或编辑器。

普通本地回归 `g57-components.xml/.log` 为 **140 passed、9 skipped**；9 项跳过是本轮新增的专属 PostgreSQL 检查，其真实结果已单独计入上面的 77 项。范围包括候选清理、数据库边界和发布源码兼容性。

`g57-resource-check.json` 确认没有遗留测试数据库容器或卷；既有 Web、worker、tutor 的完整容器 ID、镜像 ID 和启动时间与测试前一致。Ruff、格式和差异检查通过。`g57-identity-docs.json` 核对九份文档的 516 个本地链接及显式锚点唯一性；相对 `49aceaa`，CPU/GPU 执行策略、控制器和 worker 四项源码契约均未变。本轮无需另做镜像来承载测试或文档变化，但 G56 已改变 worker 契约的事实仍然保留。
