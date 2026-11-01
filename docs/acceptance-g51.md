# G51：GPU 原代停机与数据库冻结

日期：2026-11-02。对应 PRD R-09/16；[设计](rfcs/022-gpu-release-coordination.md)、[决策](adr/067-gpu-drained-database-freeze.md)、[集中检查入口](deployment/container-gpu-worker.md#database-freeze)。本轮没有调用实际 Docker、启动 PostgreSQL、查询或使用 GPU，也没有改动既有站点服务。

## 已实现的边界

协调器的停止/冻结调用位置统一使用 `quiesce`，CPU 分支保留原行为。内部 GPU 分支绑定原操作、当前私有配置、完整发布代及 Web/CPU worker 名称；先确认全队列与全部物理预约为空，再沿原生命周期停止 GPU worker。等待停止不持设备或数据库行锁；停止后持原设备锁复查容器、原启动实例及 journal，再停止 Web/CPU worker、冻结原库。

数据库的 GPU 冻结路径持排空事务直到 runtime CONNECT 撤销、已有 runtime 会话终止。期间只暂时保留本事务的精确 owner 后端；其他 owner、未知连接、prepared transaction、未完成任务或孤立预约仍拒绝。事务和最后的身份检查结束后，目标库必须没有任何会话。失败不自动 thaw、重建容器、取消任务或删除预约；重试重新检查原状态，不缓存一次成功的排空结果。

原 CPU `apply`、回退及曝光后恢复的停止/冻结调用点已改用同一入口，但公共 GPU 激活拒绝仍保留。完整 GPU apply、候选失败回退和曝光后恢复仍需要继续实现与真实验收，不能将这个内部边界视作整套部署已经完成。

## 检查结果

`test-results/g51-components-final.xml/.log`：**523 passed、11 skipped**。覆盖新冻结边界、既有 CPU 发布、GPU 配置/生命周期/就绪/原库恢复/候选检查、Web 恢复与发布包核验。Ruff 检查、719 个 Python 文件格式检查及未设置 Django 环境的模块导入通过。

- 实际私有文件、原设备文件锁和五处协调器 CPU 子进程 SIGKILL 通过。中断位置包括停止意图、停止确认、Web/CPU 停止后、数据库冻结响应和最终确认；父进程确认信号并回收自有子进程。重试不重复创建或启动 worker，仍检查原实例及原数据库。
- SQL 模型覆盖持锁撤销 CONNECT、精确 owner 后端例外、未知会话、prepared transaction、原暂停、待执行任务和孤立物理预约，以及权限已关闭后的响应丢失。模型不证明实际 PostgreSQL 语义。
- 11 项实际 PostgreSQL 检查在 SQLite 环境跳过：原有的六项配置/就绪锁检查，以及新增五项使用独立自有数据库和角色的 CONNECT、会话、行锁及重试检查。新测试只定义了可重放入口，未执行，不计为通过。
- F 两镜像与 G42 四镜像归档的只读完整核验通过，记录在 `g51-old-archives.json`；没有导入镜像或执行归档代码。源码身份见 `g51-policy-identity.json`：CPU/GPU 执行策略与 GPU 控制器未变化；`scripts/deploy_database.py` 属于 worker 源码范围，worker 契约因此变化，后续必须构建匹配的新制品。
- README 的产品能力和部署入口已核对，本轮未改变其启动命令；集中部署页、开发指南、需求追踪、RFC/ADR 和变更记录已同步。本轮未改界面、编辑器 bundle、译文或数据库迁移。

## 保留的失败与限制

`g51-freeze-before.xml/.log` 记录新增冻结接口尚不存在时的 9 项失败，以及 CPU 冻结会正确拒绝持锁 owner 连接的一项通过。实现后的 `g51-freeze-initial` 为 179 passed，`g51-quiescence-initial` 为 17 passed，均是与最终检查重叠的中间结果，不累计。

`g51-service-binding-before.xml/.log` 保留两项实际发现的绑定缺口：仅核对 GPU 上下文时，更改 Web/CPU worker 名称仍会先停止 GPU worker。现已冻结完整输入、对照原发布记录和规范服务名，拒绝发生在任何停止前；两项回归包含在最终结果中。

首次组合命令误写了不存在的 `test_release_gpu.py`，`g51-components.log` 明确记录没有运行任何测试；最终命令使用实际存在的 `test_gpu_release.py`。五项 PostgreSQL 收集跳过另记录在 `g51-postgres-collection.xml/.log`。

Docker、代理、PostgreSQL 和设备状态为明确测试模型，没有 receipt、CUDA 成绩或物理恢复证明。旧镜像缓存恢复仍等待维护者授权；完整 CPU 发布恢复矩阵、实际 PostgreSQL 冻结、GPU 队列与完整发布流程均不能据此宣称通过。用户要求暂不占用 GPU，继续有效。
