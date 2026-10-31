# G26 数据库预约与持久容器恢复

日期：2026-10-31。实现前基线为 `main` 的 `207ed76`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)和 [ADR-042](adr/042-container-recovery-database-fence.md)；操作集中在 [GPU 指南](deployment/gpu.md#container-database)。本批只使用一次性数据库、合成预约和固定 CPU guest，没有占用 GPU 或重建已有站点。

宿主 worker 可通过成对参数接入原 journal，在启动、凭证续检和取消恢复时先停止原容器，再按同一数据库拥有者和预约归档。数据库失败不阻止尝试停止已记录实例；错配、归档失败和事后数据库变化不产生就绪、成绩或额度释放。每卡启动维护身份统一生成，清理后的报告仍为 `ready: false`。

## 实际 PostgreSQL 与 CPU 容器

`test-results/px-container-database-ee7046e34bf4/result.json` 的总结果为 `passed: true`，170 项通过、无失败或跳过。原始输出见该目录的 `postgres.log`、`postgres.xml` 和 `test-results/g26-database-initial.log`。其中九个场景实际调用 rootless Docker、runsc 和 PostgreSQL，其余覆盖已有资源预约、启动、就绪、worker、执行及准入组件。

| 真实场景 | 观察结果 |
| --- | --- |
| 运行中恢复，分别保留未消费和已消费额度 | 原 CPU guest 与进程树停止，容器移除并归档；合成设备仍隔离，预约及预算不变 |
| 创建成功、完整 ID 写入前中断 | 找回同一原容器后先保存完整 ID，再停止并清理；该场景未启动 guest |
| 删除账号 | 提交外键消失后仍按设备行保存的原任务 UUID 恢复，不释放预约 |
| 数据库预约错配 | 停止原容器后拒绝归档；活动记录与不同的数据库预约保留。测试结束时另行清理自有对象，不计为正式恢复成功 |
| PostgreSQL 行锁竞争 | 真实接管事务以 SQLSTATE `55P03` 超时；原忙碌 guest 仍被停止，原记录与预约保留，解除锁后重试成功 |
| 归档写入中断 | 原容器已移除，活动记录保留 `removed` 阶段；重试归档成功 |
| 新锁拥有者接管 | 重新取得同一物理锁并接管数据库后，按原安装身份清理原实例 |
| 归档后的数据库拥有者变化 | 最后一次数据库核对拒绝成功，归档和新数据库状态保留 |

共九个真实任务容器，其中八个运行固定 CPU guest。guest 报告无 GPU 节点；没有编译学员源码或将合成设备标记为硬件已通过。全部原进程树、自有容器、临时工作目录、测试数据库/卷和任务镜像标签已由验收入口核对清理。逐场景结果区分恢复动作和测试清理。

| 身份 | 值 |
| --- | --- |
| 任务镜像 | `sha256:ec993ba7d38e19c9d20b29a72995f951962e078745ffe9aa4c02bec8069330c3` |
| 任务源码契约 | `00ac3a0f9d44df0b071960061332560e9561ac755b2f96092ea57adfb245eff1` |
| GPU 正式执行策略 | `65380fc1995e1f5f885562eb8f8999ab32e37a5bb262276ee0775cfa84efce5b` |
| CPU/WASI 策略 | `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，与 G25 相同 |

GPU 策略纳入新的数据库恢复模块和容器身份实现，旧 GPU receipt 不可直接沿用。本批没有生成新 GPU receipt。

## 组件与原始失败

容器监督、运行时、通信、journal、控制端、GPU 凭证及发布兼容回归 247 项通过、无跳过，见 `test-results/g26-components-final.log` 和 XML。

旧 journal 的七组创建/运行/升级/替换/归档回归及每组后的新 CPU guest 调用通过，见 `test-results/px-container-journal-24fd77d45586/result.json`。容器化控制端的七组 UID/锁/挂载拒绝及跨容器、宿主双向接管也通过，见 `test-results/px-container-controller-5b59616f3f86/result.json`。两者均按本批新的任务源码契约构建；控制镜像为 `sha256:90949b26fb3cf1eefa78d55a55b5a5b62c08527ff4be8bc1387d3c2c705bd537`，控制端源码契约为 `49bda58c7ff709a13ae3bff0074d9a526c351df5744174a6f4747dbcbe5f13b6`。

独立资源查询确认三次真实验收合计 38 个自有容器及四个测试镜像标签均已不存在，三份报告源码摘要与当前实现一致；原有 Web、CPU worker 和 tutor 均运行且健康，见 `test-results/g26-audit.json`。Ruff、552 个 Python 文件格式、迁移检查和既有 50 节点/26 题静态检查通过；静态检查未执行参考程序。新 CLI 参数与九份文档的 338 个本地链接/两个新锚点已核对，记录在 `g26-worker-help.txt`、`g26-database-help.txt` 和 `g26-document-links.json`。

最初生命周期检查有两处测试夹具错误，见 `g26-components-initial.log`：一处试图创建违反现有设备约束的“ready 且仍有预约”行；一处遗漏原维护服务在 `finally` 中停止的最后事件。修正夹具和预期后 112 项通过、1 项 PostgreSQL 专属检查跳过，见 `g26-components-second.log`；新增参数传递与取消恢复专项 31 项通过、1 项同类跳过，见 `g26-wiring.log`。这些跳过没有计为通过，后续真实 PostgreSQL 联合验收包含该锁竞争检查且无跳过。

## 验证范围

新入口验证数据库与真实 CPU 容器的停止、持久身份和恢复关系。宿主 GPU 运行阶段在组件测试中使用明确的替身；它不证明 CUDA 执行、设备隔离或性能。G25 容器化控制端尚未与数据库合成常驻 GPU worker；当前可选参数只让宿主 worker 清理已有容器记录。

完整设备注入与占用监测、生产资源契约、容器后端的完整启动/领取/结算、CUDA 正确性与性能、GPU Compose 及发布升级仍待交付。未重放完整 WASI、全部 PostgreSQL、Compose 或硬件验收；这些边界继续保留在[需求追踪](requirements.md)。
