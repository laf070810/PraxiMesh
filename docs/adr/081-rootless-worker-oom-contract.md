# ADR-081：固定 rootless worker 的有效 OOM 设置

日期：2026-11-15。状态：采纳。问题与方案见 [RFC-027](../rfcs/027-rootless-worker-oom-contract.md)。

GPU 协调器的新生命周期记录使用 schema 2。创建前从原私有 Docker socket 的内核 peer、原 UID/GID、进程出生时间、socket inode 和 engine ID 读取并固定 daemon OOM 默认值，显式传入非负有效分数。新启动前必须再次匹配原观察，不能通过刷新记录接受 daemon 变化。

只有 `OomKillDisable=false/null` 两种保持 OOM killer 启用的表示共享摘要。分数、其他完整配置、原容器 ID 和启动实例仍须精确核对；CPU/Web 摘要不变。配置发布的停止库存使用同一校验函数。

schema 1 原摘要保留。旧无设备预检的有限恢复只允许原操作、维护路由、暂停数据库、原节点锁、正常退出的原预检，以及从未启动的原 worker 同时成立；固定版本 daemon 的实际 OOM 观察与两字段逆变换须重现完整旧摘要。恢复证明先落盘，绑定原代、配置、ID、原始及观察摘要、完整退出状态与来源，只能继续停止和退役，不能重启或获得就绪。

这不改变 worker 可见的私有配置、执行策略或 GPU 任务协议，故不扩展 worker 镜像源码契约。已有冻结包仍按原范围核验；宿主协调器的新日志不能授予公共 GPU 激活许可。真实整站检查按 [RFC-026](../rfcs/026-gpu-site-hardware-rehearsal.md)继续，历史失败和恢复结果保留。
