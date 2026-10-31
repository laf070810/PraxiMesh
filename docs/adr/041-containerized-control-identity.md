# ADR-041：可信控制端容器与共用宿主身份

日期：2026-10-31。状态：接受开发路径；正式 GPU worker 和 Compose 集成继续待交付。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)，真实结果见 [G25](../acceptance-g25.md)，操作统一在 [GPU 指南](../deployment/gpu.md#container-controller)。

将可信容器控制代码及固定 Docker CLI 29.9.2 放入独立镜像。它连接显式指定的本机 rootless daemon，不运行嵌套 daemon，不需要宿主 Python、Podman、用户总线或普通用户 systemd 服务。镜像按完整 ID 使用，固定基础镜像、CLI 归档/二进制、源码及入口；CLI 的上游 LICENSE 和 NOTICE 随镜像保存。此镜像目前只提供固定 CPU 生命周期验收入口，不把宿主 `gpu_worker` 命令包装后宣称已完成 GPU 容器部署。

控制端镜像使用 UID/GID 0，这是 rootless 用户命名空间内的身份。入口读取内核 UID/GID 映射，必须分别映射到准备挂载清单的非 root 操作者。清单还保存持久根、锁目录、journal 和 socket 的 dev/inode 及 daemon ID；私有目录/文件仍为 0700/0600，跨宿主和容器直接使用同一锁 inode 与 journal 安装身份。拒绝 rootful/未知映射、错误拥有者、变化的挂载身份或 daemon；不改文件拥有者，不引入第二套设备锁。

只向可信控制端挂载专用状态目录和精确的 socket 文件。它保持只读根、无能力、no-new-privileges、无网络、私有 PID/cgroup；内核 cgroup 文件必须给出 1 CPU、512 MiB、无 swap、128 任务上限。任务容器仍沿用既有 2 CPU/2 GiB/512 任务总限制、UID 65532、无宿主挂载、无网络及独立绝对期限。两层资源分别核对；本决定没有定义生产 GPU guest 子配额。

Docker socket 代表对所选 daemon 的完整控制权，readonly 挂载不会限制 Docker API；必须在可信执行平面使用专用 rootless daemon，不能把此镜像或 socket 暴露给 Web、题目参考解或学员代码。这一信任边界依据 [Docker rootless 机制](https://docs.docker.com/engine/security/rootless/)与 [bind mount 说明](https://docs.docker.com/engine/storage/bind-mounts/)，具体 UID、锁和恢复行为必须通过本项目实际检查确认。

控制端退出只释放文件锁，不代表任务结束。新控制端按私有持久记录清理原容器，原 PID 1 的绝对期限继续独立生效。恢复结果仍为 `ready: false`，不能释放数据库预约或签发 receipt。验收须覆盖宿主/容器及容器之间的锁竞争、错误 UID/锁/socket 拒绝、运行中 SIGKILL 后跨容器及双向宿主接管、新 CPU guest 和原进程树消失。GPU 设备注入、持续占用监测、数据库事务、完整启动/资源/内核检查及发布协调不在本组件的成功声明内。
