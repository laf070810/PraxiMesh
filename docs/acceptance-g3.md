# G3：GPU 预约、所有权与租约组件

日期：2026-10-30。设计见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md)和 [ADR-018](adr/018-gpu-device-ownership.md)。本增量不执行新的 GPU 工作负载，不开放 GPU 提交；实际 GPU 0 内核与故障证据仍分别属于 [G1](acceptance-g1.md)和 [G2](acceptance-g2.md)。部署操作继续统一在 [GPU 指南](deployment/gpu.md)。

## 实现范围

- `GPUDevice` 记录整卡所有权、状态、任务 UUID 与租约令牌；主机私有文件锁防止同一卡被重复控制器同时拥有。
- 任务结算、租约过期、账号删除及部署恢复均保留设备预约，直到持锁控制器确认物理恢复。旧所有权和旧任务令牌不能释放新预约。
- `gpu-slot-seconds-v1` 与 CPU 调用额度分别统计；Run 90 秒、Submit 180 秒，首次执行后保留已消耗额度。账号导出只包含本人账目。
- CPU worker 拒绝 GPU 任务。Web 只读设备表，worker 不能删除设备、修改题目或管理账户；新表授权随实际迁移启用。
- CPU/GPU 都在取得任务锁后重新检查租约期限。评测策略摘要纳入模型、GPU 预约和主机锁代码；旧 receipt 不能用于新策略。

## 原始失败与修复

首轮 SQLite 定向检查 `g3-resources-initial.xml` 为 83 passed、2 skipped；跳过项需要 PostgreSQL，未计为通过。

真实 PostgreSQL 首轮 `g3-postgres-initial.xml` 为 72 passed、6 failed。三项角色检查发现关联查询默认将只读 `core_problemversion` 一同加入 `FOR UPDATE`；修正为仅锁任务行，没有增加 worker 的题目写权限。三项 GPU 锁等待检查发现，租约在等待任务锁期间过期后仍能开始执行、续租或结算。

进一步重放 `g3-postgres-lease-reproduction.xml` 为 15 passed、6 failed：角色问题已修复，同样的过期问题也在既有 CPU 三个入口复现。测试使用真实独立数据库连接持有任务行锁，并通过 `pg_stat_activity` 确认等待；只推进时间值以跨越截止时间，不模拟数据库锁。随后统一在持锁后重新检查期限，成绩写入再次核对未过期租约。

修复后 `g3-postgres-fixed.xml` 为 **139 passed、1 skipped**，唯一跳过项为 SQLite 专属超时恢复检查。范围包含真实主机锁、GPU 资源、CPU 资源、最小角色、历史迁移、部署角色与数据库锁等待。临时 PostgreSQL 容器及卷已清理。

最终 SQLite 应用回归 `g3-application-final.xml` 为 **225 passed、17 skipped**；这 17 项均为 PostgreSQL 专属场景，已由上述 PostgreSQL 回归覆盖。Ruff、格式、内容静态校验、迁移一致性与文档本地链接检查通过。

## 共用 CPU 执行路径实测

首次 `g3-wasi.log` 通过 103 个探针后，在 Agent 正确解返回时间超限，未生成 receipt。单独重放 `g3-agent-diagnostic.json` 的四次记录均在首轮消耗约 1.00 秒子进程 CPU，未执行工具动作。所选旧 `var/wasi-functions-b2` 的标准库没有字节码；与已验收 Compose bundle 相比，清单中唯一改变的制品文件是 `stdlib/python314.zip`。

改用 `px-compose-e8bf0f80f8d9/wasi` 的固定 bundle，原有执行限额保持不变。该包包含 558 个标准库字节码条目，来源和两组正确解的真实结果记录在 `g3-agent-corrected-bundle.json`。随后 **129 个真实探针全部通过**，新 receipt 为 `g3-wasi-final-receipt.json`，已核对当前策略一致性：

- bundle 清单 SHA-256：`ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`。
- 当前评测策略摘要：`0f471fa8819325d70a04366283d448aefaa885d6ebf88b0b7c9ac467bcc28a53`。
- 探针集合摘要：`be65f9e7674192ff36b3e4c1c8e489b3bef8ff6cb4bed2ce89ff7e270c8dbbd1`。

`g3-real-queue.xml` 的 **7 项真实 PostgreSQL/WASI 回归通过**：并发提交、独占领取、暂停与策略路由，以及真实 Agent worker 结算/API/本地公开执行、worker 死亡后的驱动回收。复用的是本轮刚完成的真实 receipt，没有伪造或放宽启动门槛；临时数据库容器及卷已清理。原站点健康仍为 HTTP 200、`evaluation: available`。

## 尚未覆盖

GPU 调度测试使用合成任务和真实文件锁、数据库；恢复接口测试不证明实际 GPU 已恢复。正式 worker、GPU 提交限额、完整资源/取消/失联处理、启动 receipt、可信性能、题目及浏览器/部署闭环仍待接入。当前已运行的 Compose 镜像和旧发布包未因此自动更新，也不复用其验收结果作为新策略证明。
