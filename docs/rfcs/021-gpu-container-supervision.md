# RFC-021：GPU 容器部署的独立任务监督

日期：2026-10-31。状态：分阶段实施；尚不开放 GPU 容器评测。

## 问题与最终边界

GPU 宿主 worker 依靠用户 systemd 服务、持久设备锁、私有通信和独立硬期限。把该命令直接放进现有 Web 镜像无法保留服务身份、cgroup、路径和崩溃接管语义。完整交付仍须包括 Compose/发布协调、固定运行制品、当前启动凭证、GPU 隔离与独占检查、真实队列及异常恢复，不能用容器内有 Python 或能看到显卡代替。

继续保持 Web 不接触 Docker socket；GPU 可信控制器与学员 guest 分开。普通 OCI 容器只承载可信监督代码，学员代码必须进入另行验证的 gVisor 环境。现有宿主路径在新路径全部验收前保持可用；不把开发组件接入提交准入或签发凭证。

## 方案比较

- 向控制器容器暴露宿主用户总线并继续启动宿主命令，需要跨容器匹配解释器、挂载、PID 和 UID；仍依赖宿主 Python，不能作为完整容器交付。
- 在容器内运行完整 systemd/嵌套 daemon 增加权限和运维面。Docker 的 rootless 嵌套示例仍要求 privileged，不能据此推断适合本项目。[Docker rootless 指南](https://docs.docker.com/engine/security/rootless/tips/)
- 候选方案是专用 rootless Docker 上的可信监督容器：控制器按不可变镜像和持久预约身份创建任务容器，容器 PID 1 独立维护绝对截止时间。设备隔离、guest 运行时与容器化控制器的身份映射另行接入和实测。

本阶段先实现第三种方案的监督基础，真实验证父控制器退出、整棵任务进程树、独立期限、停止与重启接管。它是完整部署所需的组件，不是新的学员执行器。

## 监督契约

客户端只连接明确选择、由当前用户拥有的本机 Unix socket，并核对 rootless、cgroup v2 和限额能力。使用空白 Docker 客户端配置，避免代理或身份配置进入容器；不改 daemon、用户服务或驱动。

每个任务由设备锁、任务 UUID、预约 UUID 和用途确定名称与标签；绑定实际容器 ID 后不能因同名替换而继续操作。镜像必须是本机已经存在的完整 SHA-256 ID，不自动拉取或接受浮动标签。创建与启动分开，启动前核对完整配置。命令仅来自可信控制器，不接受学员命令、源码或数据库凭证。

监督容器采用独立 PID/cgroup 命名空间、无网络、只读根、无额外能力、no-new-privileges、固定非 root 用户和有界私有 tmpfs；不挂宿主目录、socket 或 GPU。CPU、内存、swap 和进程数量必须在容器内读取实际 cgroup 文件核对，不能仅信任 Docker 参数。此阶段的固定 CPU 探针不代表 GPU guest 隔离。

PID 1 在绝对启动期限后拒绝启动；终止期限不因重连、观察超时或恢复而延长。仅监督器生成有限的私有状态，子进程输出丢弃，Docker 日志关闭。PID 1 退出结束该 PID 命名空间的进程树；上层仍须确认容器实际停止，再删除自己的容器。API 超时、配置不符、替换或无法确认停止均保留待恢复状态，不能据此释放设备预约。Docker 的 init、PID namespace、资源和停止选项以[官方运行文档](https://docs.docker.com/reference/cli/docker/container/run/)为依据，并以实际故障检查核验。

## 后续集成与发布门槛

### 容器内 runsc 可行性验证

2026-10-31 的固定 CPU 前提检查确认：当前监督容器内 `unshare(CLONE_NEWUSER)` 返回 EPERM，cgroup 为只读。现有依赖 RootlessKit、Podman 及可写子 cgroup 的宿主运行时不能原样搬入。原始结果在 `test-results/g22-namespace-prerequisite.json`；没有修改主机服务或执行 GPU。

下一步以独立、未接入正式评测的实验镜像验证直接 OCI runsc：固定完整运行时归档，构建只读 guest 根目录；仅对可信外层程序提供经过审阅的命名空间系统调用配置，继续使用 rootless、无额外能力、无宿主挂载、无网络和无设备。不能使用 privileged 或 unconfined 代替确定所需权限。gVisor 的[无特权嵌套运行说明](https://gvisor.dev/docs/user_guide/rootless/)和 [OCI 入口](https://gvisor.dev/docs/user_guide/quick_start/oci/)提供机制依据，实际可用性仍以本机探针为准；外层 syscall 配置参照 [Docker seccomp 说明](https://docs.docker.com/engine/security/seccomp/)。

实验必须明确资源属于哪一层：外层现有 2 CPU、2 GiB、512 任务上限覆盖监督器和所有 guest；这不能冒充宿主路径独立的 0.5 CPU、512 MiB guest 配额。接入正式评测前须选定并版本化资源契约、证明内存/进程/CPU 故障和独立期限，重新验证执行环境及凭证。外层期限必须仍覆盖 runsc、gofer、Sentry 和脱离父进程的后代，不能仅终止附着 CLI。CPU 探针只能证明所执行的基础路径，不能启用 GPU、正式成绩或普通容器回退。

必须解决并验证：容器化 worker 与既有设备锁/UID/主机身份兼容；持久保存并恢复旧镜像和容器身份；可信通信、取消和租约；gVisor/NVIDIA 单卡注入、PID 对应和持续占用检测；完整启动与资源/内核/故障探针；Compose 数据库初始化与发布协调器的排空、升级和回退。GPU 驱动仍在信任边界内，Docker GPU 选项不能替代 [gVisor GPU 检查](https://gvisor.dev/docs/user_guide/gpu/)。

### 受信控制连接

G22 已实际验证容器内 CPU guest，但只从固定探针文件收集结果，尚不能传送任务。下一步复用有界 `Channel` 协议，通过不可变容器 ID 的 `docker exec --interactive` 启动镜像内固定桥接程序；禁用 TTY，不把任务放进命令行、环境或 Docker 日志，不向任务容器挂载宿主通信目录。Docker 对 exec 生命周期与环境继承的说明见[官方接口](https://docs.docker.com/reference/cli/docker/container/exec/)，是否正确传递 EOF、背压与故障须实际验证。

桥接程序只连接容器私有 tmpfs 中的固定 Unix socket。双方核对内核对端 UID/PID；桥接端必须连接监督状态指定的主控制器，控制器只接受固定桥接入口。宿主先核对已持锁的容器 ID、启动实例和完整运行配置，再校验原启动 nonce、子进程身份及绝对期限，完成确认后才传任务。每个调用只允许一个连接；断连不能重连继续旧任务，也不能延长原期限。

本地连接与字节转发分别受期限、累计字节数及固定缓冲区限制。关闭连接、终止 Docker CLI 和确认 guest/整个容器退出是不同动作；取消或租约失效先停止发送，再沿原容器身份回收。应真实验证大消息、guest 不可见通信端点、运行中断连/取消、额外输入和硬期限，并核对新的独立调用可以恢复。此路径仍不处理设备注入、不生成 GPU 凭证、不直接替换宿主通信。

本阶段不修改当前执行策略、设备允许列表、默认题库或现有服务；不占用 GPU，不设置 privileged，不以普通容器运行学员代码。后续任何需要放宽监督容器权限的方案必须重新记录取舍和验证，不能悄悄扩大此契约。
