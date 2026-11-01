# G62：完整应用双数据库回归

日期：2026-11-02。对应 PRD R-09、AC-07；起始提交 `9da2bdd`，修复提交 `a3af0607a33aeaaca5952874a31a735f0fe8a751`。本轮只修改测试断言和文档，应用、内容、迁移、评测策略和运行制品未改。操作沿用 [README 的开发检查](../README.md#开发与检查)与[独立 PostgreSQL 检查](deployment/production.md#database-validation)。

## 原始失败与修正

首次完整 SQLite 尝试发现 `tests/test_challenges.py::test_english_controls_and_discovery` 仍查找旧的 `Maximum reservation units per formal submission`。题库已按 CPU 调用单位与 GPU 整卡时段分别显示资源额度，当前英文翻译为 `Maximum CPU invocation units per submission`；页面行为符合现有资源契约。

确认失败后，对本次已核对身份的 pytest 进程发送一次 SIGINT，让它输出当前失败并清理状态。`test-results/g62-sqlite.xml/.log` 保留 **1 failed、174 passed、1 skipped**；这是主动中断的部分结果，不是完整套件完成或超时。`g62-sqlite-run.json` 确认退出码 2 和临时数据库清理。

仅更新过期的英文断言，保留挑战入口、成就与 ML 筛选检查，不恢复含糊的旧资源文案。原失败用例重放 **1 passed**，见 `g62-challenge-fixed.xml/.log`；随后从已提交的 `a3af060` 分别重放两套完整应用检查。

## 完整结果与覆盖范围

| 环境 | 通过 | 跳过 | 失败/错误 | pytest 耗时 |
| --- | ---: | ---: | ---: | --- |
| 一次性 SQLite | 3697 | 194 | 0 / 0 | 722.79 秒 |
| 自有临时 PostgreSQL | 3787 | 104 | 0 / 0 | 996.29 秒 |

两轮均收集 3891 个用例。原始报告为 `g62-sqlite-fixed-1.xml/.log` 和 `g62-postgres.xml/.log`，进程结果与清理分别记录在同名前缀的 `-run.json` 中；逐项对照和 XML 摘要见 `g62-application-matrix.json`。

SQLite 跳过的 **91 项 PostgreSQL 专属检查全部通过**，包括实际行锁与超时、资源竞争、最小角色权限、组织发布锁、分享可见性、GPU 元数据准入及恢复事务、数据库冻结/复制和历史 schema 迁移。PostgreSQL 单独跳过的一项 SQLite busy timeout 检查已在 SQLite 通过。GPU 设备、成绩和环境数据仍为合成夹具；真实数据库通过不证明 GPU 执行或性能。

一项错误 owner token 用例在收集时生成随机 UUID，导致两份 XML 的参数名称不同。核对 `tests/test_deploy_gpu_check.py` 的同一参数行后，汇总只对这一项显式对应，保留两份原名；没有按名称模糊合并其他用例，也没有合并两个环境的通过数当作独立用例总量。

两轮共同跳过 **103 项**：85 项需要显式开启真实 WASI，18 项需要固定 CPU 任务镜像与 PostgreSQL 联合环境。本轮两个外部集成开关均关闭，这些跳过不算通过。当前冻结 CPU 发布包的真实 WASI/浏览器证据见 [G61](acceptance-g61.md)，容器与数据库联合检查入口见 [GPU 部署指南](deployment/gpu.md#container-database)；本轮没有重新执行这些验收。

## 清理与交付边界

SQLite 使用本次私有临时目录；PostgreSQL 使用现有独立检查入口创建带随机标签的容器、卷和数据库，以及空白 Docker 客户端配置。`g62-postgres-before.json` 与 `g62-postgres-run.json` 确认临时容器、网络和卷无残留，原 Web、worker、tutor 的完整 ID、镜像及启动时间均未变。

修改文件的 Ruff、格式检查通过。CPU/GPU 执行策略、控制器和 worker 源码契约未改，文档链接及差异检查记录在 `g62-identity-docs.json`。没有访问 GPU、加载旧 F 应用镜像、更新现有服务或推送。托管 CI 仍需要最终提交的远端结果；旧 F 应用整站升级、真实 GPU 和其他外部门槛继续按[需求追踪](requirements.md)保留。
