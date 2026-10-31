# G25 容器化可信控制端、UID 与跨容器恢复

日期：2026-10-31。实现前基线为 `main` 的 `0468265`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)和 [ADR-041](adr/041-containerized-control-identity.md)，操作集中在 [GPU 指南](deployment/gpu.md#container-controller)。本批只运行固定 CPU guest，没有使用 GPU、修改共享 daemon 或重建现有站点。

新增独立控制端镜像和 `ContainerController`：固定 Docker CLI、源码及入口，读取实际内核映射和 cgroup 限额；按私有挂载清单核对同一设备锁、journal 和 rootless socket。可信控制端能够创建、通信和恢复原任务容器，任务容器仍没有宿主目录、socket、网络或 GPU。

## 实际结果

最终报告 `test-results/px-container-controller-a328a7ac9aab/result.json` 的七组检查及总 `passed` 均为 `true`；源码摘要与最终实现一致。原始输出在 `test-results/g25-controller-third.log`。

| 检查 | 实际观察 |
| --- | --- |
| 共享锁与 UID/GID | 控制端内 UID/GID 0 实际映射到宿主 2031/601；安装身份、锁目录 inode、持久 owner 和容器名称与宿主一致。容器持锁时宿主及第二容器均拒绝，宿主持锁时容器也拒绝；SIGKILL 后可重新取得同一锁 |
| 错误用户 | UID/GID 65532 无法读取镜像内私有入口文件，Python 以 2 退出；没有创建任务或活动记录 |
| 锁目录替换 | 临时目录换成新 inode 后，入口以 `SandboxUnavailable` 停止；没有创建任务 |
| socket 替换 | 挂载测试自有的另一 Unix socket，入口在调用 daemon 前拒绝；没有创建任务 |
| 容器接管容器 | 原控制端被 SIGKILL 时固定 guest 仍在运行；新控制端按同一 journal 清理原实例，再通过新 nonce/ID 执行并完成真实 CPU guest |
| 宿主接管容器 | 原控制端被 SIGKILL 后宿主取得同一锁、确认安装身份并清理原运行任务；恢复返回 `ready: false` |
| 容器接管宿主 | 宿主启动固定 guest 并关闭连接、释放锁；新控制端清理原任务，再完成新的 CPU guest 调用 |

内核读取的控制端限额为 `cpu.max=100000 100000`、`memory.max=536870912`、`memory.swap.max=0`、`pids.max=128`。控制端使用固定 runc、只读根、无能力、no-new-privileges、私有 PID/cgroup 和无网络；只有专用状态目录及指定 socket 两个挂载。这里的 socket 提供所选 daemon 的完整控制权，readonly 挂载不构成 API 授权限制。

五个真实任务容器分别包含三次待接管调用和两次恢复后的新调用。检查记录了原宿主 PID/启动身份及消失状态；新 guest 报告无 GPU 节点、不可写根、无能力、不可见私有通信路径及无法连接宿主回环端口。最终 5 个任务容器、10 个控制容器全部移除，两个临时镜像标签和 `/tmp` 下的自有状态目录已移除；合成锁、journal 与报告副本留在私有结果目录。

| 制品 | 固定身份 |
| --- | --- |
| 控制端镜像 | `sha256:494c176c812fa1d686cd39c3c71231ae28f833c32317abc93adb822715ea082c` |
| 控制端源码契约 | `968d73982afd451b75e45f5824dd9dfe0f3b26e786f1bdfa591cba3d4970e3e9` |
| 任务镜像 | `sha256:082fae2a13ee3db7a41502a06b7e86923e96d267af5c8fab4fe9a68897a0855a` |
| 任务源码契约 | `3e6d2d3821bcc12aa33bf64581ef160ca5a7128fc3dedc76392dc5bc2f047549` |
| Docker CLI 29.9.2 归档 | SHA-256 `9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb` |
| CLI 二进制 | SHA-256 `6c71cfe71faff949a602946a4df4799b563232e79d0b8b6fd343d0ef081c9621` |

组件、通信、锁和发布兼容检查 244 项通过、无跳过，见 `test-results/g25-components-final.log` 及 XML。Ruff、544 个 Python 文件格式、无待生成迁移和既有 50 节点/26 题静态检查通过；参考程序没有通过静态检查执行。命令参数及本地文档链接已核对，结果分别在 `test-results/g25-cli-help.txt`、`g25-document-links.json`；最终资源、源码与正式策略摘要复核在 `g25-audit.json`。

最终独立查询确认三轮合计 20 个自有容器及六个测试标签均已不存在，成功报告与当前源码匹配；原有 Web、CPU worker 和 tutor 三个服务仍运行且健康。测试没有替换它们的镜像或数据。

## 原始失败和修复

首轮 `test-results/px-container-controller-a7b6c0cba99a/result.json` 在控制端启动时失败，尚无任务容器。Docker 的 local 日志驱动默认压缩与 `max-file=1` 冲突，daemon 返回 `compression cannot be enabled when max file count is 1`。按本次完整容器 ID 筛选的原始 daemon 错误保存在 `test-results/g25-original-start-error.log`；修正为显式关闭压缩，仍保留单个 1 MiB 日志上限和固定错误类别输出。该控制容器、镜像标签及存储均已清理。

额外边界测试先复现了四种缺失实际 cgroup 限额仍被接受的情况，见 `test-results/g25-limits-before.log`。入口现读取内核文件并逐项匹配控制端资源契约，不能只相信 Docker create 参数；修复后的组件检查及实际容器都通过。

第二轮 `test-results/px-container-controller-fe8f41fa2258/result.json` 已通过真实 UID、限额及双向锁竞争，但错误 UID 场景的断言错误：它在 Python 读取 0700 镜像目录中的入口前就被文件权限拒绝，退出码为 2；验收原先要求进入应用后以 1 退出。保留权限，修正断言并同时收集固定容器的 stdout/stderr。该轮四个控制容器及标签、存储均已清理；第三轮完整重放通过。

## 验证范围

这是可信控制端与已有容器执行组件的实际 CPU 验证，没有连接应用数据库、读写 GPU、生成 receipt 或放宽提交准入。当前 Docker 29.9.2 rootless/cgroup v2 环境已验证；状态目录位于 `/tmp` 的 XFS，报告目录为 NFS，没有重放 NFS bind mount 下的跨命名空间锁。G24 的宿主 NFS 原子发布结果不能替代这项场景。

数据库预约和就绪事务、GPU 设备注入与进程/占用监测、生产资源契约、完整启动及 CUDA 正确性/性能、常驻 worker 和 Compose/发布升级仍待完成。CPU/GPU 正式执行策略摘要与 G24 相同；未重放完整 PostgreSQL/WASI、G22 资源矩阵或 Compose/浏览器，原站点未替换。
