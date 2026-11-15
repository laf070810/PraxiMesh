# ADR-092：优先淘汰原嵌套 sandbox

日期：2026-11-15。状态：实现与无设备检查通过，真实 GPU 重放待完成。背景见 [RFC-038](../rfcs/038-nested-sandbox-oom-priority.md)。

`container_oci.specification()` 为固定私有 guest 添加 `process.oomScoreAdj=1000`，由固定 runsc 对原 sandbox 和 gofer 应用。值不来自环境、公开包或学员输入，原 OCI 配置改变后仍拒绝启动。外层监督器、可信控制进程、其他容器及宿主进程均不调整；不新增权限，也不降低任何进程的淘汰优先级。

原 512 MiB 整任务预算、CPU/进程/存储限额、截止时间、版本化资源 profile 与 journal 恢复语义不变。只能在实际本 cgroup OOM 证据、原 guest 与进程树清理及新的正确恢复均确认后返回资源结果；整个外层任务被杀或恢复失败仍不可返回成功。

351 项运行时、资源、驱动、编译、调用链与故障检查通过，记录为 `test-results/nested-oom-priority-components-2026-11-15-1.log`。标准无设备资源矩阵正常资源、恢复失败与新正确调用三组通过；内核采样确认 sandbox/gofer 的分数为 1000，外层 PID 1 和可信控制进程保持 200。OOM 后原 sandbox 退出、内存释放及恢复通过，原目录和镜像标签已清理。证据为 `test-results/resource-oom-priority-diagnostic-2026-11-15/` 与 `test-results/px-container-kernel-3aa9217618ec/result.json`；原 200 分数基线也完整保留。

CPU 对照证明设置生效及该路径的恢复，不证明 GPU 上的第二次 OOM 已解决。源码身份改变，后续使用新的匹配控制器/任务镜像，重新执行完整 GPU 启动，不沿用旧 receipt 或省略原内存探针。
