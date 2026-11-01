# G56：整站 GPU 发布编排与恢复方向

日期：2026-11-02。对应 PRD R-09/16；[设计](rfcs/022-gpu-release-coordination.md)、[决策](adr/072-coordinated-gpu-release-recovery.md)、[集中检查入口](deployment/container-gpu-worker.md#whole-site-release)。本轮未运行实际 Docker、PostgreSQL 或 GPU，也未重建既有站点。

## 已实现与当前限制

内部整站流程连接原有排空、维护、冻结、独立数据库复制或初始化、迁移、最小权限、CPU/GPU 候选准备与持久曝光。记录完整前代和候选代、原冻结包路径以及每次恢复的阶段；覆盖初次 GPU 安装、CPU→GPU、GPU→GPU 和 GPU→CPU。升级保留已有内容，只有明确的首次安装可导入示例包。

曝光前先恢复实际配置所在原库的工作，清理原验证轮，再冻结和退出或切换配置，最后回到前代；失败候选无需先通过同一产品检查才能回退。原 worker 从未启动时不为退出配置启动 GPU 工作，排空、原对象和冻结保护仍保留。初次安装失败也保留数据库，不认领无原 owner/marker 的库。

曝光决定之后保留当前库和开放后的写入，原恢复尝试继续使用同一实例和验证协议。曝光响应丢失时不重启仍运行的原 worker；确认同一实例退出才重新进入维护和原库恢复。CPU 目标重试只接受维护路由或已决定的原 Web 端口，不重复激活已生效路由。跨操作恢复只读核对已完整发布的原配置，但不能越过未完成退出。

公开 GPU `apply/recover` 仍拒绝执行，普通 CPU 路径也拒绝新的整站记录。此次交付是内部编排及其验证，不是可用的 GPU 发布方案；真实 PostgreSQL 锁/权限、Docker 服务组合、完整 CUDA 启动和队列、升级/回退/恢复矩阵仍须完成。

## 验证边界与原始失败

最终 `test-results/g56-components-final.xml/.log`：**784 passed、14 skipped**，耗时 212.59 秒。覆盖整站编排、原发布/准备/曝光/退出/恢复/冻结、CPU 协调器、Web 恢复和发布制品校验。14 项真实 PostgreSQL 专项因 SQLite 环境跳过：7 项配置/快照、5 项冻结和 2 项候选事务，不能计为通过。

后续 [G57](acceptance-g57.md)已在独立 PostgreSQL 实例实际执行这 14 项，并补充完整迁移 schema 的权限与复制检查；上述 G56 日志继续保留当时的原始范围。

整站模型使用实际原子文件、协调器文件锁和自有 CPU 子进程，数据库内容、Docker、代理、CPU/GPU 服务及成绩为显式模型。35 个整站场景覆盖四种安装/升级方向、曝光前后失败、未启动候选退出、清理失败保留原库、未标记数据库、归档与日志损坏、内容不重导、未知路由、活跃原 worker 的重试和退出后的重新恢复。

其中十一处实际 SIGKILL 分别覆盖发布的 copying/migrating/preparing/exposing、代理开放后的写入，以及恢复的 freeze_source/retire_source/drain_target/prepare_target/expose_target/finished。父进程确认收到的信号并回收子进程，从实际保存的文件继续，检查不复制旧数据、不重启已经退休的候选。服务模型不能证明实际数据库复制、CUDA 清理或硬件正确性。

另有两项组合检查直接串接本仓库的原配置/worker、恢复、候选轮次与曝光组件，实际读取冻结源码 tar、私有配置、原锁和日志；完整镜像校验、Docker、数据库快照与候选成绩仍为原夹具明确的模型。检查保留原曝光历史、新验证轮次及开放后写入，并覆盖恢复曝光后原 worker 停止的再次恢复。五项数据库边界检查确认查询只读、精确参数和 owner/marker 校验，不把缺失与未标记混为一谈；这不是 PostgreSQL 实测。

原始记录均保留在忽略目录 `test-results/`：

- `g56-release-initial.xml/.log`：17 项早期失败来自整站测试夹具缺少 `ensure_directory`；子进程尚未到达信号点，这一轮不算 SIGKILL 验收。补上原锁的目录接口后，`g56-release-second.xml/.log` 的首批 17 项通过。
- `g56-new-operation-before.xml/.log`：复现未完成配置退出被只读恢复路径绕过；另一项是将幂等 CPU 服务确认误判为副作用的测试断言。修复退出检查并核对不重复 GPU 启动、发布、Web 替换和队列验证后，两项通过，见 `g56-new-operation-fixed.xml/.log`。
- `g56-exposure-exit-before.xml/.log`：复现恢复曝光期间原 worker 退出后只重试曝光、未重新恢复的问题；已按原实例状态分流。
- `g56-cpu-ack-before.xml/.log`：CPU 路由已经生效但响应丢失时，重试错误地要求维护路由；已只接受持久决定中的原端口，未知端口继续拒绝。
- `g56-flow-expanded.xml/.log` 与 `g56-flow-fixed.xml/.log`：组合夹具先在锁前访问数据库、随后没有将候选管理命令交回原命令模型；两处修正后组合检查通过，见 `g56-flow-components.xml/.log`。
- `g56-focused-final.xml/.log`：137 项专项检查通过。最终相关矩阵记录在 `g56-components-final.xml/.log`，专项数量不与最终矩阵累加。

## 文档、界面与制品

部署操作继续集中在 `docs/deployment/`；首页说明当前可用方案，GPU 专页区分内部编排和公共入口。README 的能力与启动说明仍适用，AGENTS 已记录新的恢复约束。当前界面的中英、25 页、四宽度共 200 组布局及编辑器浏览器复查通过，单独记录在[界面验收](acceptance-editor.md)，此次没有更改界面资源。

本轮没有变更模型、内容或数据库 schema，也没有构建、导入、改写既有镜像或发布包。实际 GPU 准入、性能及正式题型仍按[需求追踪](requirements.md)保留完整门槛。

Ruff、格式、无 Django 设置的部署模块导入及差异检查通过。`g56-identity-docs.json` 核对 11 份文档的 528 个本地链接、显式锚点唯一性及本轮文件摘要。相对 `934670a`，CPU/GPU 执行策略和控制器源码契约未变；worker 契约仅因 `scripts/deploy_database.py` 改变，下一轮实际部署须构建新的匹配制品，旧归档只保持核验兼容。
