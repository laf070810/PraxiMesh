# ADR-017：在已委派的用户子树中验证 GPU 基础环境

状态：接受，仅适用于基础设施检查，2026-10-30。关联 [RFC-018](../rfcs/018-gpu-environment-probe.md)。

普通 rootless Docker 的资源限额已恢复，但固定 runsc 的 systemd 路径尝试连接系统管理器；直接 cgroupfs 路径又尝试写系统根 cgroup。两者都不适合当前已委派的普通用户权限。

检查脚本采用临时用户服务、RootlessKit 的用户/PID/cgroup/挂载/网络命名空间和独立 Podman 存储。固定 gVisor 完整归档先验证摘要；CDI 仅保留明确选定的单卡 UUID。可信控制端在专属父 cgroup 写入并读回资源预算，执行后再次核对，并用实际 CPU 忙循环检查节流。原有 daemon 和站点不参与试验。

必须以非 root 用户运行，目标卡由维护者授权独占；已有计算任务时退出，不终止未知任务。GPU 容器保留 runsc、非 root、只读文件系统、网络关闭、去除 capabilities 和 `no-new-privileges`。不使用忽略 cgroup 错误或放行未知驱动的开关。

此决定已由单卡识别、厂商 CUDA 样例、CPU 节流和资源清理实测支持。它不决定正式 worker 的部署架构，不生成评测 receipt，也不开放 GPU 题型；正式 GPU 契约、显存与故障预算、调度和成绩证据另行实施和验收。运行命令集中在 [GPU 部署指南](../deployment/gpu.md)。
