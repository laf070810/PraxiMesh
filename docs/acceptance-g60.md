# G60：历史 F 数据库结构到当前版本的迁移验证

日期：2026-11-02。对应 PRD R-09，基线 `df87b28`；操作入口集中在[部署数据库检查](deployment/production.md#database-validation)。本轮只新增测试和文档，未更改应用、历史迁移、运行制品或评测策略。

## 来源与验证边界

对 `var/releases/2026-10-24-f-candidate-1` 进行只读完整归档核验，其可信清单 SHA-256 为 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482`，与 [F 验收](acceptance-f.md)一致。记录在 `test-results/g60-f-baseline.json`；没有导入镜像，也没有执行归档中的 Python。

冻结源码的 17 个项目迁移与当前仓库逐字节相同；Django 同为 5.2.18，`content/starter.json` 摘要也相同。检查清单保存在 `tests/fixtures/f_migration_history.json`。测试使用当前仓库中这些未改写的迁移及其历史模型，独立重建 F 数据库结构；迁移图的依赖闭包和框架目标均实际核对。之后执行当前新增的三个迁移：

- `core/0004_gpu_resources`：设备记录和预算约束。
- `core/0005_gpu_admission_environment`：设备环境发布字段。
- `showcase/0002_shared_metric_scope`：分享指标范围及旧写入默认值。

这验证重建的历史数据库到当前结构的迁移。它未运行旧 F 应用或旧镜像，没有真实旧站点数据、HTTP 流量或评测；不能据此将整站跨版本升级标为完成。GPU 表只写入明确的合成隔离状态记录，不读取或使用硬件，也不生成就绪凭证。

## 实际数据库检查

复用 `tests/test_deploy_database_postgres.py` 的自有容器身份检查，为每个场景建立独立随机数据库与角色；数据库 owner/marker、回环地址、空库及关闭评测等条件确认后才建立历史 schema。结束时按原身份删除测试数据库、角色、容器和卷，不接触既有站点。

合成数据包含账号、会话、未签发令牌、私有草稿/笔记/计划/社区内容、收藏、组织及固定课程引用，一道题的两个不可变版本，以及 legacy/reserved/consumed/released 四种 CPU 预算形态。提交记录明确标记为测试夹具，没有 Accepted 成绩或代码执行；分享记录只用于检查原公开标志和范围保留。

实际步骤和断言如下：

- 原 Web 角色写入私有草稿，worker 可以读取提交，但不能读取账号；随后通过实际协调器冻结原库。
- 容器中的真实 `pg_dump/pg_restore` 复制到另一自有数据库。迁移前完整行、列及序列签名与原库完全相同；原库始终保持冻结和原值。
- 在目标库执行真实当前迁移。逐表投影全部原列，按主键用 PostgreSQL binary COPY 比较字节及 SHA-256；原有权限、内容类型和迁移记录也逐行保留，允许新增系统记录。
- 所有原列的类型、空值约束和顺序不变；所有原序列定义不变，业务序列状态精确相同。系统表序列仅允许随新记录前进；没有重导或替换原题库和版本。
- 旧分享行均得到 `sequential-v1`，公开标志和修订号不变；实际旧式 INSERT 省略新增字段时仍得到同一默认范围，未知范围被数据库拒绝。
- 四种 CPU 预约形态保留；未知策略、零单位、缺失日期或 consumed 缺失执行时间等非法更新被真实约束拒绝。
- 新设备表最初为空。worker 可写入合成的 quarantined 记录，但不能删除设备或读取私有草稿；Web 可读设备状态，但不能写设备。目标库冻结/解冻后，原库仍保持原值且未开放。

## 结果、原始失败与清理

最终 `test-results/g60-postgres-fixed.xml/.log`：**4 passed、0 skipped**，耗时 23.49 秒。包含一项迁移来源/图检查及三个独立的真实数据库场景；重复的建立、复制和迁移不累加为独立通过项。

初次 `g60-postgres-initial.xml/.log` 为 1 passed、3 errors：合成令牌未声明任何用途，触发历史数据库原有的 `practice_token_has_scope` 约束，测试尚未到达复制升级。为未签发的夹具令牌设置读取本人结果用途后重放通过；没有放宽模型或数据库约束。SQLite 的 `g60-inventory.xml/.log` 为 1 passed、3 skipped，仅能证明来源清单和跳过条件，不能计作数据库通过。

`g60-postgres-fixed.json` 确认最终临时容器和卷均已清理；现有 Web、worker、tutor 的完整 ID、镜像和启动时间与 `g60-existing-services-before.json` 一致。没有恢复旧镜像缓存、重建现有服务、修改共享 daemon 或访问 GPU。

界面源码相对最近浏览器验收未变。本轮复核其 200 组中英布局记录，并查看桌面社区、平板目标选项、320 px 英文令牌及学习页截图，未发现新增对齐问题；没有重新运行浏览器或更新旧服务，历史范围见[编辑器与页面对齐验收](acceptance-editor.md)。

Ruff、格式及差异检查通过。`g60-identity-docs.json` 核对十份文档的 492 个本地链接、显式锚点唯一性及本轮文件摘要；相对 `df87b28`，CPU/GPU 策略、控制器和 worker 四项源码契约均未改变。
