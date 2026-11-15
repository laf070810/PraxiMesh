# 独立临时站点的 GPU 发布验收

更新：2026-11-15。**GPU 网站部署尚未验收通过，公共激活入口仍关闭。** 本页记录 [RFC-026](rfcs/026-gpu-site-hardware-rehearsal.md) 的真实临时站点检查；[本地完整 GPU 启动与公开样例](acceptance-gpu0-startup.md)已有独立通过结果。

## 首次 CPU→GPU 尝试与原站点恢复

采用源提交 `75e2762` 的[同源 CPU/四镜像包](acceptance-current-releases.md#uvm-release)，新建 PostgreSQL、回环 HTTPS、合成账号和独立 GPU 节点。原 CPU 版本的实际提交、草稿及目录快照通过；普通 GPU worker 创建完成但未启动。

固定无设备预检实际退出码为 0，却被协调器的创建摘要核对拒绝。完整配置仅有 Docker 29.9.2 的两项正常启动变化：OOM 分数 `0→200`，OOM killer 禁用字段 `false→null`。两字段的精确逆变换重现原完整摘要；原发布操作、数据库、容器和节点均保留，没有另建站点绕过。

[RFC-027](rfcs/027-rootless-worker-oom-contract.md) / [ADR-081](adr/081-rootless-worker-oom-contract.md) 的修复提交为 `645f0dd`：新创建时固定 daemon 的有效 OOM 设置；旧预检只追加退役证明。472 项相关回归通过；真实恢复首次又发现预检在正常首次启动前曾执行停止，因此原 `started_at` 是零时间而非空值。保留该失败，补齐两种从未启动状态后，72 项定向检查通过。

随后从**同一个发布操作**恢复成功：原预检及从未启动的 GPU worker 停止、原 GPU 配置退出、原 CPU 库/版本恢复、回环 HTTPS 健康通过，操作终态为 `rolled_back`。耗时 346.330 秒；最终 GPU 0 空闲，已有 Web、CPU worker、tutor 的完整身份、启动时间及健康状态与恢复前一致。临时站点保留，供后续验收使用。

私有证据在 `test-results/uvm-releases-2026-11-15/`：`owned-gpu-site-1/`、`legacy-preflight-recovery-1/`、`legacy-preflight-recovery-2/`。恢复后报告保存原/新发布快照和两次服务观察；原失败不覆盖。此结果证明该次曝光前失败的真实恢复，不证明完整 GPU 发布故障矩阵。

## 包含 OOM 修复的新冻结包

源提交 `645f0dda0f4f961348c8f6eb7b4e7e55393172b1`，992 个文件逐字节匹配 Git，source ID `e65c07547ffe58cb7634cb14a07138e30e5900eefffe7e32c400b0833dea3aae`。

| 格式 | 不可变目录 | release.json SHA-256 |
| --- | --- | --- |
| CPU | `var/releases/2026-11-15-cpu-upgrade-target-6/` | `816d2cc230395d2d6220d092c2832fdfff712eae9f50407757c9c8dc06d520e8` |
| GPU | `var/releases/2026-11-15-oom-four-image-1/` | `622e36b4a968115e4f286b9662c35c0327e79c993adce7f001595de47b3e84bc` |

两包同源、Web/CPU 镜像和评测配置相同；GPU worker 与 task 镜像沿用完全相同的已核验字节。完整核验、标准导入及 10 项无设备 worker/Compose/PostgreSQL 检查通过。CPU 应用首轮通过启动探针与部分浏览器流程后，因调用者未提供本地 CLI 所需的匹配 WASI 环境而停止；补齐并提前校验 bundle 后，在新目录完成完整重放，433.774 秒、退出码 0。129 项启动探针、三组基础/七组扩展浏览器、实际提交与资源清理均通过，报告为 `process-final.json` 和 `cpu-application-2/release-acceptance.json`。

GPU 构建在完整制品保存后，清理唯一临时镜像标签时失败：原预检仍引用同一 GPU worker 镜像。原制品不重写，独立核验后继续；临时标签及镜像保留。构建清理已补充按完整 ID/标签及实际容器引用检查，使用中的最后一个标签保留并写入独立 `cleanup.json`，其他标签仍使用 `--no-prune`。98 项相关组件检查通过，实际保留路径确认原镜像元数据未变；没有停止引用该镜像的容器或强制删除缓存。

新包及构建原始失败在 `test-results/oom-releases-2026-11-15/`；`source-and-pair-audit.json` 保存源提交与镜像身份，`cleanup-reproduction.json` 和 `cleanup-safe-retention.json` 保存实际标签检查。源码包本身仍是原 `645f0dd`，不包含随后补充的构建清理逻辑。

## 正式 worker 的首次完整启动

原临时站点继续采用这批冻结包，新 schema 2 无设备预检通过，正式 GPU worker 启动成功。首次启动及随后原操作恢复均完成 21 个阶段：初始恢复、资源检查和全部 19 项内核检查，包括实际超时、非法地址、取消及附着进程终止。

两次均在首个控制器故障检查失败，后两项未执行；没有签发就绪或开放提交。第二次原任务的 Docker 事件确认独立退出码为预期的 70，但完整检查仍未通过，具体失败条件待定位。现场为 `owned-gpu-site-2/`，最小事件观察仅保存容器 ID、时间和退出码，不保存命令/环境内容。原临时库、配置、容器和任务历史继续保留。

此时原 CPU 回退也等待同一 GPU 完整启动，因此新增[有限中止路径](rfcs/028-failed-gpu-startup-cpu-rollback.md)。300 项相关组件、补充后的 26 项边界检查及 39 项真实 PostgreSQL 冻结/配置检查通过；临时 PostgreSQL 资源已清理。原站点只读资格预检通过，发布日志字节未变；实际中止回退尚未执行。

## 待完成的真实范围

继续原站点，依次完成 CPU→GPU、正式 API 参考解/错解/恢复正确解、GPU→GPU、GPU→CPU，以及曝光前后的实际中断和恢复。分别核对历史数据、会话、GPU 环境、正式性能、物理预约和清理。全部成功前，仍按[需求追踪](requirements.md)保留网站 GPU 发布门槛。
