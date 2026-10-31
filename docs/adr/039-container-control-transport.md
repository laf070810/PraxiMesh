# ADR-039：通过固定容器身份建立受信控制通道

日期：2026-10-31。状态：接受开发路径，实际结果见 [G23](../acceptance-g23.md)。背景见 [RFC-021](../rfcs/021-gpu-container-supervision.md)。尚未接入正式 GPU worker。

容器内 runsc 不再依赖宿主通信目录。宿主 `ContainerConnection` 持有原设备锁，通过受验证的本机 rootless Docker socket，在完整容器 ID 中启动固定镜像内的桥接入口；不用 shell、TTY、浮动镜像名或任意程序参数。客户端使用空白 Docker 配置与最小环境，任务仅通过流发送。任务容器继续断网、无主机挂载、无 Docker socket、无数据库凭证。

主控制器在自己的私有 tmpfs 内建立一次性 Unix socket。桥接端核对 `SO_PEERCRED` 中的 UID/GID、监督状态指定的主 PID 和私有 cgroup；主控制器核对桥接端的 UID/GID、cgroup 和固定命令。宿主核对原启动 nonce、主 PID、期限和容器启动实例后才确认通道。容器镜像及运行配置由 G22 的源码契约绑定。这些校验建立受信基础设施连接，不能让任意容器或 guest 自报身份获得准入。

应用消息复用原 `Channel` 的四字节长度、严格 JSON、4 MiB 单消息、8 MiB 单向累计预算和期限。桥接器只转发字节，两向各有 64 KiB 缓冲与含帧头的累计上限；输入 EOF 关闭对端，输出 EOF 先排空已收数据。每次监督调用只接受一次连接，断连后不得续接旧任务或更新原期限。

租约回调在通信等待期间继续执行；失效后调用者退出连接上下文，停止传输。关闭连接或杀死 Docker CLI 都不能证明 guest 清理；必须仍按原容器身份确认内部清理或外层停止、进程消失，再处理预约。最外层 PID 1 的绝对期限独立于连接与回调。

固定 CPU 验收覆盖大消息往返、guest 不可见 socket 路径、启动身份错配、超长帧、运行中断连、客户端退出、租约取消、额外输入、期限与新调用恢复。不接收任意源码，不执行 CUDA，不产生启动凭证。设备注入、容器化 worker 身份映射、版本化资源契约、完整启动和部署协调仍需交付。操作集中在 [GPU 指南](../deployment/gpu.md#container-transport)。
