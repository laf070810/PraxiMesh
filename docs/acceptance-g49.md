# G49：原库 GPU worker 恢复与中断续跑

日期：2026-11-01。对应 PRD R-09、R-16；[设计](rfcs/022-gpu-release-coordination.md)、[决策](adr/065-original-database-gpu-worker-recovery.md)、[集中检查入口](deployment/container-gpu-worker.md#worker-recovery)。本轮不调用实际 Docker，不启动 PostgreSQL，不查询或使用 GPU，不更改既有站点服务。

## 实现与验证边界

内部恢复将原实例停止、新预检、同一容器启动及完整就绪观察连接为持久流程。输入必须匹配当前私有配置及原发布代；曝光意图存在时，只允许恢复已曝光的当前代。流程不切换或冻结数据库，不改代理，不删除任务 journal 或物理预约。预检须属于本次操作，且开始不早于上一 worker 的实际退出；原来的成功不能跨停止或跨操作复用。

恢复记录绑定完整原容器 ID、运行契约、配置摘要、操作及尝试。六个阶段中断后沿同一记录继续，仍在运行的启动保留原期限；已经确认退出的本次启动，只在下一次调用开始新尝试。启动后确认尚未落盘的情况，先核对原意图并保存失败，再允许后续重试。每份记录最多十六次 worker 尝试，每次最多十六个失败预检，达到边界时保留现场。成功阶段仍重新检查就绪，未完成恢复阻止配置发布。

公共 CPU apply/recover/restore/expose 在任何变更前拒绝已有 GPU 配置、容器或恢复记录，包括目标包仅包含 CPU 的情况。只绑定节点、尚未创建 GPU 配置和容器的 CPU 站点保持原行为。

最终相关回归 **761 passed、6 skipped**，见 `test-results/g49-final-components.xml/.log`。实际临时文件、文件锁、不可变测试归档和协调器 CPU 子进程参与检查；Docker、数据库、设备状态及就绪发布均使用显式模型。六项 PostgreSQL 专项在 SQLite 环境中跳过，不计为通过。测试中的 ready 状态不构成实际 GPU 就绪、清理或部署成功证据。

## 关键结果

- 六处实际协调器 SIGKILL 覆盖 stopping、resetting_check、checking、starting、waiting、ready；父进程确认被终止的自有 PID，重新读取持久文件并续跑。模型验证原容器 ID 保留，不重复创建、启动或停止；已退出的预检仅重置生命周期记录，无需额外停止命令。
- 原数据库和私有配置保持不变，数据库切换、冻结、复制及健康状态重写在夹具中明确禁止。未清理的任务标记和物理预约保留，等待重试不续期，不自动重启仍在运行的实例。
- 已退出 worker 和失败预检可在后续调用沿原实例重试并保留记录；其他操作、曝光前旧库、缺失原容器、损坏阶段证据和未完成恢复不能被认领或绕过。旧 ready 阶段仍要求当前就绪；就绪返回后退出也不能确认恢复成功。
- `g49-preflight-regression.xml/.log` 保留两项旧预检被错误接受的失败；`g49-gate-before.xml/.log` 保留四项公共 CPU 路径忽略已有 GPU 状态的失败。修复后的对应场景包含在最终回归中。
- `g49-lost-start-before.xml/.log` 保留启动已退出、确认丢失后无法恢复的一项失败。修复先沿原 start_requested 记录确认失败，本次不重启，后续调用再开始新尝试；原始尝试和容器身份保留。

中间的 `g49-preflight-before` 含一处测试插入位置错误，`g49-recovery-initial` 的七项失败来自对已退出预检多算一次 Docker stop；这些不是生产缺陷。`g49-foundation`、`g49-core`、`g49-components` 分别为 152、122、760 项通过；它们与最终回归重叠，不累计为独立检查数。

旧制品只读核验通过，见 `test-results/g49-old-archives.json`：F 清单 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` 的 464 个源码文件与两个镜像；G42 清单 `44b6ca03b1e22d92382e5a04b578dee64813f57eceb1b3c3485117b0fdfa2142` 的 839 个源码文件与四个镜像及完整层。本轮未导入镜像或执行归档代码。

源码身份在 `test-results/g49-policy-identity.json`；父提交为 `28c5c05`。CPU 策略和 GPU execution/controller/worker 源码范围均未改变，本轮变更位于宿主发布协调器。旧归档通过不表示支持新恢复流程或已完成 GPU 激活。

全仓库 Ruff 静态与格式检查通过，9 份相关文档的 438 个本地链接核对通过，结果见 `test-results/g49-docs.json`。部署步骤继续集中在部署指南，README 仅保留入口。

## 未完成事项

此内部组件尚未连接完整 GPU apply、候选队列验证、升级回滚与曝光流程；这些仍有实现工作。真实 Docker/PostgreSQL 生命周期与锁、完整 GPU 启动、正确/错误队列、物理恢复及升级验收也未完成，不开放正式 GPU 提交或发布候选内容。

此前恢复旧测试镜像的自动审批拒绝仍未获授权解除，本轮未重试加载镜像或启动测试数据库；[G44](acceptance-g44.md) 的真实 Web 恢复和完整 CPU 发布复验继续待完成。托管 CI/runner、长期部署目标和公开运营/授权事项见[需求追踪](requirements.md)。
