# RFC-027：rootless worker 的启动 OOM 配置

日期：2026-11-15。状态：实施中，真实回归尚未完成。

## 已复现的问题

RFC-026 的自有临时站点在首次预检启动后拒绝原容器。预检实际退出码为 0；完整配置只有 `OomScoreAdj: 0→200` 和 `OomKillDisable: false→null` 两项变化。把观察副本中的这两项恢复为创建值，所得完整摘要与原持久记录一致。原站点、数据库、容器和发布操作均保留，普通 GPU worker 尚未启动。

当前 daemon 是 Docker 29.9.2、rootless、systemd/cgroup v2。其私有 Unix socket 的内核 peer 身份指向原非 root 用户的 daemon；实际 `/proc/<peer>/oom_score_adj` 为 200。[固定 Moby 源码](https://github.com/moby/moby/blob/docker-v29.9.2/daemon/internal/rootless/specconv/specconv_linux.go)将较小的 OOM 分数提高到 daemon 的值；[资源校验](https://github.com/moby/moby/blob/docker-v29.9.2/daemon/daemon_unix.go)会把不支持的 OOM killer 禁用字段归一为空。这里没有修改 daemon，也不能把调整 daemon 作为部署前提。

## 新创建记录

协调器使用生命周期 schema 2。在落创建意图前，从原私有 socket 的 `SO_PEERCRED` 获取 peer，以原 UID/GID、进程出生时间、socket inode 和 engine ID 绑定实际观察。读取 daemon 的 OOM 分数，显式传入 `max(0, score)`；首次启动和重启前重新核对原观察，不自动更新记录。

完整运行配置摘要仅把 `OomKillDisable` 的 `false/null` 两种保持 OOM killer 启用的表示归一；缺失、`true` 和其他类型均拒绝。`OomScoreAdj` 必须与创建意图完全一致，其他配置仍逐字参与摘要。停止库存、配置发布和生命周期读取使用同一规则；CPU/Web 的摘要不变。

## 旧预检的有限恢复

不改写 schema 1 的创建摘要。仅在原操作的实际维护路由与暂停数据库下，持原节点锁，且原普通 worker 确实从未启动时，允许为原已正常退出的无设备预检追加恢复证明。预检必须仍处于原 `start_requested`，原启动前时间为零；当前 daemon 观察及两字段的精确逆变换必须重现完整旧摘要。

证明绑定原完整 ID、创建摘要、完整观察摘要、启动实例、代、配置、owner、操作及 daemon 来源；先持久保存再继续正常停止/冻结/退出。重试必须核对同一观察，不能改写证明；证明只授权原对象的停止与退役，不能授权启动、预检通过、ready 或曝光。其他旧 worker 仍按原完整摘要校验，未知差异保留并拒绝。

## 验证与交付

先覆盖非零 daemon 分数、启动前后表示、后续分数/配置篡改、daemon/peer 变化、旧记录兼容、恢复证明中断与重试；再通过同一 rootless daemon 的真实无设备容器重放，并恢复保留的原临时站点。

本变化只涉及宿主协调器私有日志，不修改 worker 配置、任务协议、执行策略或 frozen worker 源码范围。新协调器须继续验证已有冻结制品；旧制品并不因此获得 GPU 激活许可。后续整站重放使用交付修复后的新发布包，保留旧包和原失败报告。
