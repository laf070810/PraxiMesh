# 暂停评测、排空与恢复

这组命令是单主机发布协调的基础。它们不切换代理、不启动或停止服务、不迁移或恢复数据库；完整部署协调器仍待完成。设计见 [RFC-005](rfcs/005-deployment-coordination.md)、[ADR-007](adr/007-admission-coordination.md)。

## 首次升级

迁移 `0002_admission_and_policy` 增加暂停 token 和任务的评测指纹。旧 Web/worker 不理解它，不能直接依靠新命令协调旧进程：首次升级先停止旧 Web 入口，等待旧 worker 完成队列，停止旧 worker，备份并迁移，再启动新版本。不能同时保留旧 worker。历史完成记录不改；没有指纹的遗留待处理任务不会被新 worker 猜测重跑，按已有超时规则记为平台故障。

应用和 worker 使用同一发布包，并保持部署中的文件不可变。新任务的指纹结合 backend、解释器摘要与策略代码；worker 只领取与自身启动指纹匹配的任务。解释器不变而策略变化仍产生新指纹，必须重新探测和启动 worker。

## 后续暂停与排空

通过维护通道使用当前发布包的管理命令及数据库维护凭证；不要给 Web 页面增加维护权限，不要把 owner 凭证传入判题 guest。为一次操作产生 UUID，并在私有部署记录中保存它供重试：

```sh
export PRAXIMESH_DEPLOYMENT_TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4())')"
.venv/bin/python manage.py judge_admission status
.venv/bin/python manage.py judge_admission drain \
    --token "$PRAXIMESH_DEPLOYMENT_TOKEN" --timeout 180
```

`drain` 先原子暂停新 Run/Submit，再等待所有 queued/running 任务结束；用户仍可阅读、登录、保存草稿并查看原任务结果。返回 JSON 包含暂停所有权、等待/运行数量及当前策略 worker 是否健康，不包含代码、题目测试、密码或数据库 URL。只需要关闭入口时使用 `pause`，它不等待任务结束。

同一 token 可重试，其他 token 不能夺取或解除暂停。超时、Ctrl-C 或维护进程退出后仍暂停；命令不取消任务、不伪造结果，不自动强杀 worker。先检查 worker、租约和 `incompatible_pending`，保持正常的 `maintain` 计划任务。没有健康 worker 时，排空可能超时；不能以删任务或手改结果让检查通过。

确认排空后，后续部署步骤才可停止旧 worker 并处理兼容性、备份、迁移和候选验证。新版本开始接受用户写入后，不可盲目切回旧数据库快照。现阶段这些动作仍按运维手册执行，不能把排空成功等同于发布成功。

## 恢复入口

候选或恢复后的版本完成实际隔离探针、健康检查和功能验证后，用原 token 恢复：

```sh
.venv/bin/python manage.py judge_admission resume \
    --token "$PRAXIMESH_DEPLOYMENT_TOKEN"
.venv/bin/python manage.py judge_admission status
```

恢复会拒绝错误 token、关闭的评测配置、失联或其他策略的 worker，以及尚存的其他策略任务。暂停本身不会让 worker 停止；不要把它当成宿主执行隔离或数据库整体只读模式。

## 复验

```sh
# PostgreSQL 并发矩阵；使用独立容器和显式数据库卷并自动清理。
.venv/bin/python scripts/postgres_tests.py -q
# 已配置真实 WASI 后，把 sandbox 回归也包含进同一次运行。
RUN_SANDBOX_TESTS=1 .venv/bin/python scripts/postgres_tests.py -q
# 最新发布包的真实排空/恢复随容器浏览器验收一起验证。
.venv/bin/python scripts/release_acceptance.py \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
```

真实场景先提交有界的标准库函数，让 `drain --timeout 0` 确实超时；检查新提交 503、阅读和草稿可用，等待原任务实际 Accepted，拒绝错误 token，再恢复并执行另一份正确提交。测试自己的账号和数据库均清理，不使用注入的评测结果。
