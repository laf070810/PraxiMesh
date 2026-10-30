# GPU 评测：现状与接入准备

[部署方案选择](README.md) · **GPU 正式评测尚未交付；本页提供环境和内核组件检查，不是 GPU worker 的部署入口。** 当前 Compose 和发布协调器提供七种 CPU/WASI 评测。添加 `--gpus`、安装 NVIDIA runtime 或注册 Actions runner 都不会自动增加 GPU 题型。

## 本机当前状态

2026-10-30 维护者补齐 CPU 委派并重启用户 Docker 后，当前 context `/run/user/2031/docker.sock` 的 `MemoryLimit`、`SwapLimit`、`PidsLimit`、`CpuCfsQuota` 均为 `true`，资源控制警告为空。可信临时容器设置 `--cpus=0.5`，实际运行 5 秒消耗约 2.501 秒 CPU，`cpu.max=50000 100000`，发生 50 次节流；测试容器已清理。CPU 配额缺口已解决，结果见 `test-results/cpu-controller-restored/result.json`。

维护者明确指定第 0 张 RTX 3090 供独占验收，按 UUID 选择设备。固定 gVisor `release-20261026.0` 与驱动 `590.48.01` 的实际组合已跑通：在独立用户 cgroup 命名空间、Podman、runsc 与 CDI 中，只识别目标卡，并完成 50,000 元素 CUDA 向量加法。最终 CPU 探针用时 9.002 秒、消耗 4.393 秒 CPU，发生 75 次节流，0.5 核限额生效。临时资源已清理，原站点正常。

初次接入失败和修正见 [RFC-018](../rfcs/018-gpu-environment-probe.md#实际结果与下一步)。之后实现了有界 CUDA 内核契约和受信驱动，完成 [G1 的 7 组正确性检查](../acceptance-g1.md)，以及 [G2 的 9 组设备故障、终止与恢复检查](../acceptance-g2.md)。[G3](../acceptance-g3.md)增加了主机设备锁、持久预约、独立账目与租约组件；正式 GPU worker 尚未调用这些组件，GPU 提交仍关闭。完整资源/失联策略、性能比较与产品接入仍待完成。

G3 的 `gpu-slot-seconds-v1` 按 Run 90 秒、Submit 180 秒预约整卡时段，默认每日账目上限 `JUDGE_DAILY_GPU_SECONDS=1800`，与 CPU 调用单位分开。它不是实际测得的 GPU 时间；此配置目前只用于调度组件，不能据此启用 GPU 评测。正式部署还须固定共享锁目录、执行指纹和启动检查，并验证执行总时限符合预约。账号删除只删除用户数据，设备的运行身份保留到受信控制器确认恢复，不能手工清空预约来恢复提交。

[G4](../acceptance-g4.md)新增交互式执行器和控制器批次计时，目前只有协议与拒绝路径的自动检查结果。首次真实重放发现目标卡存在其他账号的计算进程，在创建服务前退出；这些新路径尚未完成 GPU 验收。G1/G2 的历史通过记录不替代新代码的重放。

[G5](../acceptance-g5.md)补充任务服务硬期限、整进程树清理及有界数据库回调。固定 CPU 进程树和真实 PostgreSQL 检查已通过；它们尚未与 GPU 运行环境合成正式 worker，也不改变 Compose 的可用题型。

[G6](../acceptance-g6.md)进一步验证了真实 RootlessKit 命名空间与外层服务的私有通信、环境变量清理、断连及期限终止。这些检查不占 GPU；正式 Podman/runsc 会话、真实设备恢复和 worker 仍需连接及验收。

[G7](../acceptance-g7.md)已把共用离线 Podman/runsc 环境、受限管道和容器回收接入控制链，并通过固定 CPU guest 的七项实际检查。CUDA 会话入口已有实现，尚未完成新 GPU 硬件重放、设备恢复和正式 worker 接入。

[G8](../acceptance-g8.md)增加控制器异常退出后的私有存储回收，真实 CPU 容器的退出、终止及期限场景通过。该组件只恢复文件系统，设备预约仍须等待实际 GPU 恢复；正式 worker 尚未接入。

[G9](../acceptance-g9.md)已实现运行适配器、私有 CUDA 控制器与数据库结算连接。协议及实际 PostgreSQL 边界通过，真实命名空间的检查仅覆盖准备前拒绝；完整 GPU 运行、独立正确解恢复、时间预算、正式 worker 和启动 receipt 尚未验收。

[G10](../acceptance-g10.md)连接启动接管与异常恢复：先停止数据库旧预约对应的服务，以及每卡固定身份的遗留维护服务，再清理私有目录、运行固定正确解。恢复成功仍保持隔离停用和旧预约，完整就绪检查及启动凭证尚未交付。所有控制器副本与后续升级必须共用原来的私有工作根和锁目录；换目录不能处理旧进程或存储，也不能据此恢复提交。

[G11](../acceptance-g11.md)把共用资源检查接入维护协议，CPU 重放已验证内存、宿主 PID、实际节流、存储与异常清理。GPU 模式还会要求准确设备可见性及独立 CUDA 正确解恢复，该路径尚未实测；完整启动凭证、worker 和正式准入仍待完成。

## 不占 GPU 的控制链检查

适用于显卡暂被占用时开发、检查 worker 控制链，或在目标 Linux 主机核对用户服务管理器及 CPU/内存/PID 委派。需要项目 Python 环境、可用的普通用户 systemd 管理器和 cgroup v2；不需要显卡、驱动或 CUDA 制品。不要用 sudo，也不在普通 Web/CPU worker 容器内执行。

```sh
.venv/bin/python scripts/gpu_control_acceptance.py
```

脚本只启动固定 CPU 休眠进程树，创建本次独有的临时服务与合成锁身份，依次验证硬期限、主进程退出、控制器退出后的接管，以及拒绝停止身份不符的服务。结束后检查全部自有服务已停止、cgroup 无进程；原始记录保留在 `test-results/px-gpu-control-*/result.json`，须有 `passed: true` 和四项 `checks`、四项成功 `cleanup`。

实际读取服务父 cgroup 的 2 核、2 GiB、零 swap 和 512 PID 限制。这不是 GPU 或显存验收，也不创建启动 receipt。配合开发测试使用现有 [PostgreSQL 验证入口](../../README.md#开发与检查)检查 `tests/test_gpu_control.py`、`tests/test_gpu_resources.py`。正式 worker 仍须在运行时验证设备占用与恢复，不能手工把数据库设备状态设为 ready。

要继续检查外层控制器与私有命名空间的连接，再准备 RootlessKit、可用的 subuid/subgid 映射及 iproute 工具，并执行：

```sh
.venv/bin/python scripts/gpu_namespace_acceptance.py
```

这一步也不需要显卡、驱动或 CUDA 制品。它使用完全断网的独立命名空间和固定 CPU 进程，验证大消息传输、断连、额外消息拒绝、独立期限及同 UID 的错误 cgroup 拒绝；检查环境变量白名单、实际父组限额与工作目录清理。结果在 `test-results/px-gpu-namespace-*/result.json`，应有五项成功 `checks`，每项 `cleanup.cgroup_empty`、`workspace_removed` 为 `true`，总 `passed` 和 `temporary_directory_removed` 为 `true`。

该脚本只运行控制进程，没有运行 Podman guest、CUDA 或 GPU 恢复探针。启动失败时保留失败结果，先核对 RootlessKit 与 `ip` 是否位于系统路径；不要通过继承整套 worker 环境变量修复，也不要把残留目录或设备预约直接删除。

修改运行适配器或控制器入口后，还可在同样的非 GPU 环境验证实际加载和错误顺序拒绝：

```sh
.venv/bin/python scripts/gpu_controller_acceptance.py
```

它不接收设备或学员源码参数，不需要 CUDA 制品；只在私有服务中启动可信入口，核对对端后发送顺序错误的请求，确认在准备运行时之前失败并退出。结果位于 `test-results/px-gpu-controller-*/result.json`；`passed`、`rejected_before_runtime_preparation`、`cleanup.cgroup_empty`、`workspace_removed`、`temporary_directory_removed` 均应为 `true`。该入口不验证 CUDA 执行或 GPU 恢复。

## 不占 GPU 的离线容器检查

适用于继续验证真实隔离容器启动、管道、资源限制和回收。除上述用户服务及命名空间依赖，还需 Podman、fuse-overlayfs、固定完整 runsc 归档和 Python OCI 归档；不需要 NVIDIA 驱动、Toolkit 或 NVRTC。本机已有这两个归档；新环境先按[固定制品准备](#提前保存固定镜像减少下载依赖)下载：

```sh
.venv/bin/python scripts/gpu_runtime_acceptance.py \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --image-archive var/gpu-preflight/python-image-20261030.oci.tar
```

检查不接受学员源码，明确关闭 nvproxy，不映射 GPU。固定 runsc guest 验证只读根文件系统、私有文件/宿主回环网络/设备隔离、输出限制、超时、取消、附着客户端异常退出、实际 CPU 节流、512 MiB 内存和 32 MiB 临时存储边界。宿主 PID 限额由私有控制器命名空间中的固定可信原生探针验证：在专用叶组实际触及父组 256 个任务的上限，再清理全部自有子进程；不会在共享主机 cgroup 中运行该探针。每个场景后启动新隔离容器检查重新运行。

结果在 `test-results/px-gpu-runtime-*/result.json`。总 `passed` 和 `controller.passed` 应为 `true`，有九项成功 `controller.checks` 和十七项 `controller.cleanup`；`controller.private_store_removed`、`service_cleanup.cgroup_empty`、`workspace_removed`、`temporary_directory_removed` 均应为 `true`。PID 检查的 `details` 应记录 255 个子进程、至少一次内核拒绝及零剩余任务；探针自身计入第 256 个任务。任一失败保留原始记录，先修正原因再重放，不手动把设备标为可用。

这个入口不会修改共享 Docker/Podman 存储或用户配置，也不生成 GPU receipt。容器正常退出和 CPU 重新运行成功，不证明 CUDA 设备已恢复；正式 GPU 提交仍关闭。

## 不占 GPU 的异常恢复检查

适用于验证控制器被强制终止后，替代进程能否回收映射 UID 的私有存储。使用与上节相同的用户服务、命名空间及离线制品依赖，串行执行：

```sh
.venv/bin/python scripts/gpu_recovery_acceptance.py \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --image-archive var/gpu-preflight/python-image-20261030.oci.tar
```

脚本在固定 CPU guest 确实运行后，分别触发任务控制器立即退出、整服务终止、独立硬期限，以及维护控制器立即退出；确认原进程树停止，再用持久身份启动新的回收服务。还验证活动任务不能回收，以及回收服务在删除前中断后的接管。它只创建并处理本次独有服务、锁和目录，不访问 GPU、不修改共享容器存储。

结果在 `test-results/px-gpu-recovery-*/result.json`，应有四项成功 `checks`；每项 `all_task_processes_stopped`、`recovery.workspace_removed`、`service_cleanup.cgroup_empty` 为 `true`，总 `passed` 和 `temporary_directory_removed` 为 `true`。回收过程核对原 UID/GID 映射及目录身份；错误、映射变化或残留挂载会保留现场。不要手工删除未知目录、调整 subuid/subgid 来绕过核对，或据此释放设备预约。下一步真实 CUDA 恢复与正式准入仍需独占卡。

启动状态与接管边界可使用一次性 PostgreSQL 检查：

```sh
.venv/bin/python scripts/postgres_tests.py -q \
  tests/test_gpu_startup.py tests/test_gpu_execution.py \
  tests/test_gpu_control.py tests/test_gpu_resources.py
```

这组检查会自动清理自有数据库容器和卷；GPU 执行使用明确的测试替身，不能作为真实设备恢复证据。尚无正式 GPU worker 启动命令，不要直接调用数据库的恢复转换来绕过就绪检查。

## 重放单卡环境检查

适用于准备 GPU 执行环境和排障；它不安装常驻 worker、不生成评测 receipt，也不会开放网站提交。需要维护者明确分配一张空闲 GPU。脚本拒绝 `all`，发现已有计算任务就退出。

所有模式共用每卡主机锁，默认目录为 `/run/user/UID/praximesh-gpu-locks`，也可显式传 `--lock-directory`。同一平台的全部控制器必须共用该私有目录；目录权限为 0700，锁文件保留，不随测试清理。它不能阻止其他账号运行 CUDA；即使 GPU 利用率为零，发现已有计算进程仍须协调释放，不能直接终止未知任务。

本机已具备：普通用户可用的 systemd 管理器及委派的 cgroup v2 控制器、RootlessKit 3.3、Podman 5.8、slirp4netns、fuse-overlayfs、NVIDIA Container Toolkit 和受固定 runsc 支持的驱动。使用项目 Python 3.11+ 环境；不要使用 sudo。其他机器先准备这些依赖与权限，并允许拉取固定的 NVIDIA 样例镜像。

本机归档已在 `var/gpu-preflight/gvisor.tar.bz2`。新环境先按[固定制品准备](#提前保存固定镜像减少下载依赖)下载完整 runsc 归档，确认单卡授权与空闲后执行：

```sh
.venv/bin/python scripts/gpu_environment_probe.py \
  --gpu 0 --runtime-archive var/gpu-preflight/gvisor.tar.bz2
```

`--gpu` 也接受完整 GPU UUID；只能指定已授权设备。整个试验有 360 秒服务时限、2 核/2 GiB/512 PID 总预算；串行工作负载的父 cgroup 限制为 0.5 核、512 MiB、零 swap、256 PID。这些 PID 包括 gofer、sentry 和驱动辅助程序开销。

脚本依次验证单卡可见性、厂商 CUDA 正确性和实际 CPU 节流，结束后删除专属容器、存储和临时服务。命令退出码须为 0；输出目录 `test-results/px-gpu-cgns-*/` 中，`result.json` 应有 `passed: true` 且无清理错误，`outer.json` 的两个 `temporary_*_removed` 应为 true。`service.log` 和 `runsc/` 保留失败细节。固定公开镜像使用临时空认证文件拉取，不继承或改写用户的 registry 凭证。

如果停在镜像拉取阶段，先检查仓库及代理连通性；下载超时不表示 GPU 不可用。本次曾出现继承代理的下载超时，直连重放成功。仅当目标网络允许直连时，可为单次命令去掉代理环境后重试，不必改动正在运行的 Docker 服务。

## 重放内核正确性检查

在上述依赖和单卡授权基础上，增加固定 NVRTC 13.1.115 归档；不需要在宿主执行 CUDA 编译器。本机已保存 `var/gpu-preflight/nvrtc-13.1.115.tar.xz`，可以直接执行第二条命令。新环境先下载：

```sh
curl --fail --location --retry 3 \
  --output var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  https://developer.download.nvidia.com/compute/cuda/redist/cuda_nvrtc/linux-x86_64/cuda_nvrtc-linux-x86_64-13.1.115-archive.tar.xz

.venv/bin/python scripts/gpu_environment_probe.py \
  --gpu 0 --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz
```

脚本在解包前验证固定 SHA-256，再校验动态库；只将最小受信驱动和编译库只读挂载进固定 Python 镜像。向量加法、矩阵乘法的参考解和针对性错解均在 gVisor 内编译和执行，预期值留在控制器。`result.json` 还应包含 7 个通过的 `kernel_probes`；此模式同样检查单卡、实际 CPU 限额和清理。

各模式串行执行，不接受任意学员源码、不生成正式 receipt，也不开放网站提交。正常内核成功不能代替非法访存、显存耗尽、超时、设备恢复和可信性能验收。实现与未完成门槛见 [RFC-019](../rfcs/019-gpu-kernel-evaluation.md)和 [G1 记录](../acceptance-g1.md)。

## 重放设备故障与恢复检查

仅在维护者专门分配的空闲单卡执行，不能与科研任务或其他评测并行。沿用上述两个固定归档，增加 `--fault-probes`：

```sh
.venv/bin/python scripts/gpu_environment_probe.py \
  --gpu 0 --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --fault-probes
```

此模式测试设备输出不能伪造协议、设备堆的小额成功/超额失败、非法访存、超过本卡容量的模块分配、非法 PTX、长运行内核终止及后续正确解恢复。必须出现 9 个通过的 `kernel_probes`，并有目标设备空闲记录和 `kernel-timeout_termination`。控制器在 8 秒期限后只终止本次创建的容器，等待实际退出；CLI 超时本身不算清理成功。

每次结束后还通过宿主 `nvidia-smi` 检查目标卡无计算进程，未知进程不会被终止，设备不会被重置。仍有计算进程时检查失败，不把设备视为可用。超出整卡容量的分配失败不证明本卡支持任意较小显存配额；当前设备堆限制为 8 MiB，模块、驱动和整卡预算仍须在正式 worker 策略中单独定义。

该入口仍是组件检查，不包括队列取消、worker 失联、租约争用或正式成绩。历史结果与尚未完成项见 [G2 记录](../acceptance-g2.md)。

## 提前保存固定镜像，减少下载依赖

离线 CPU 检查与 GPU 组件共用完整 runsc 归档；内核、故障和离线 CPU 模式还共用固定 Python OCI 归档。本机已准备下列两个文件，无需再次下载。新环境先下载完整 runsc；程序会核对内置 SHA-512 和 runsc 摘要，并保留 sidecar 二进制：

```sh
mkdir -p var/gpu-preflight
curl --fail --location --retry 3 \
  --output var/gpu-preflight/gvisor.tar.bz2 \
  https://storage.googleapis.com/gvisor/releases/release/20261026.0/x86_64/gvisor.tar.bz2
```

需要 `skopeo` 才能制作 Python 镜像归档，重放只需归档本身。保存到尚不存在的目标文件：

```sh
GODEBUG=http2client=0 GOMAXPROCS=2 skopeo \
  --override-os=linux --override-arch=amd64 --command-timeout=180s \
  copy --preserve-digests --src-no-creds --retry-times=2 \
  --image-parallel-copies=1 \
  docker://docker.io/library/python@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15 \
  oci-archive:var/gpu-preflight/python-image-20261030.oci.tar
```

使用归档重放全部设备故障检查；去掉 `--fault-probes` 即为参考解/错解模式：

```sh
.venv/bin/python scripts/gpu_environment_probe.py \
  --gpu 0 --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --image-archive var/gpu-preflight/python-image-20261030.oci.tar \
  --fault-probes
```

探针先复制归档到自有临时目录，验证固定清单及所有配置/镜像层的摘要、大小和平台，拒绝多余文件、重复成员及链接，再导入独立存储并核对实际镜像 ID。它继续运行全部真实隔离、正确性或故障检查，不复用 receipt。归档保留用于后续重放，导入后的临时存储仍会清理。

## 交互执行与批次计时检查（待真实验收）

此模式使用相同固定制品，逐条编译/执行请求，受保护答案由控制器比较。仅在独占条件恢复后运行：

```sh
.venv/bin/python scripts/gpu_environment_probe.py \
  --gpu 0 --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --image-archive var/gpu-preflight/python-image-20261030.oci.tar \
  --session-probes
```

它覆盖参考解、错解、编译/入口错误、最大输入、公开 Run、批次采样、输出上限、取消、客户端异常退出和后续参考解恢复；仍需检查退出码、`result.json` 与 `outer.json` 的实际成功和清理记录。不能和 `--fault-probes` 合并运行。会话模式关闭 runsc 和 NVRTC 的详细诊断日志。

采样条件固定为同一输入、每批 16 次、2 批预热和 5 批测量；取控制器发送请求到收到结果的批次墙钟中位数，包含通信、分配及同步成本。它不是纯 CUDA 内核时间，不代表已交付正式性能榜单。GPU worker 的每任务服务硬期限、持续占用检查、启动 receipt 和产品闭环仍待接入。

## CPU 委派问题的原因

此前 CPU 配额缺失的原因是用户服务的控制器委派：

- 内核启用 `CONFIG_CGROUPS`、`CONFIG_CGROUP_SCHED` 和 `CONFIG_CFS_BANDWIDTH`。
- 系统 cgroup v2 包含 `cpu`；但 `user@2031.service` 只获委派 `memory pids`，其 cgroup 中没有 `cpu.max`。
- rootless Docker 服务自身的 `Delegate=yes` 不能弥补上层用户管理器没有获得的 CPU 控制器。

这类问题应检查实际 Docker context 和用户服务，不能据此认定整台主机不支持 CPU 配额。Docker 官方说明了 [rootless 控制器委派要求](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)。

## 其他主机遇到 CPU 配额缺失时

优先在专用、可销毁的执行节点准备运行时和固定 GPU 设备；部署控制器、数据库凭证和科研任务不放进这个执行环境。若沿用现有共享主机，先确定专用执行账户、设备和维护窗口，系统配置由主机管理员处理。

对当前 rootless 问题，管理员可核对目标用户管理器的 systemd drop-in，按目标范围委派所需控制器。例如针对本次诊断的用户实例使用 `user@2031.service` 的覆盖配置，而非直接修改所有用户的服务模板：

```ini
[Service]
Delegate=cpu cpuset io memory pids
```

应先保留已有覆盖配置，再重载 systemd，并在维护窗口使目标用户服务及 Docker 获得新委派；是否需要重启用户管理器/daemon由实际生效情况决定。这可能影响当前 Compose 及该用户其他容器，本项目不会自动执行主机改动。单独写入配置或 `daemon-reload` 不等于运行中的服务已经获得 CPU 控制器。

修复后用目标账户和**实际执行节点所使用的 Docker context**复核：

```sh
docker context inspect --format '{{json .Endpoints.docker.Host}}'
docker info --format '{{json .SecurityOptions}}'
docker info --format '{{json .CgroupDriver}} {{json .CgroupVersion}}'
docker info --format '{{json .MemoryLimit}} {{json .SwapLimit}} {{json .PidsLimit}} {{json .CPUCfsQuota}}'
```

Docker CLI 的 Go 模板使用 `.CPUCfsQuota`；Engine API/JSON 的字段名则是 `CpuCfsQuota`。两者大小写不同。若看到 `can't evaluate field CpuCfsQuota in type system.dockerInfo`，应修正检查命令，不能据此判断控制器委派失败。本机已复现这个命令错误，修正后输出为 `true true true true`。

四项资源能力必须全部为布尔值 `true`，随后还要用受限真实任务验证 CPU、内存和 PID 上限确实生效。不能删除 `judge/sandbox.py` 的配额检查，也不能以普通容器或可由学员程序解除的 `ulimit` 替代隔离门槛。

## GPU 隔离还需要什么

现有 [gVisor 适配器](gvisor.md)只支持 Python JSON 函数。GPU 路径需要单独实现、定义策略摘要并运行相应探针。候选方案为固定版本 gVisor `runsc` 与 `nvproxy`，需逐项验证：

1. **运行时与驱动匹配**：固定 `runsc` 的来源、版本和摘要，用 `runsc nvproxy list-supported-drivers` 核对宿主驱动的精确版本，再核对 CUDA/编译器及镜像。不能只比较驱动主版本，也不能用允许未支持驱动/强制 ABI 的开关代替验收。
2. **设备兼容性**：gVisor 官方列出的已支持设备不含 RTX 3090；文档指出相同架构的消费卡可能工作，但仍需要实际试验，不能承诺安装后可用。[官方 GPU 兼容性与边界](https://gvisor.dev/docs/user_guide/gpu/)
3. **执行边界**：只暴露被预约的 GPU，限制网络、挂载、进程和输出；数据库与发布凭证留在可信控制器。`nvproxy` 仍依赖宿主 NVIDIA 驱动，驱动在信任边界内。rootless、设备注入与所选运行时的组合必须单独验证。
4. **故障回收**：验证超时、取消、worker 失联、显存耗尽、非法访问与残留任务清理。无法确认设备已恢复时停止领取任务，不能把故障设备立即分配给下一位学习者。

首轮可只验证一张独占 GPU、一次一个任务。多张卡用于增加容量之前，先记录设备身份、驱动/编译器/镜像及性能基准条件。独占整卡是资源分配策略，不自动证明显存预算或故障隔离已成立。

## 可以继续实现的产品部分

| 工作 | 完成标准 |
| --- | --- |
| GPU 题型与内容契约 | 明确接口、容差、公开样例、受保护用例和预算；准备正确实现与针对性错解，沿用现有题目不可变版本 |
| 编译与执行后端 | 编译器和学员程序都在已验证隔离内运行；可信端保留答案，不能信任 guest 自报通过或耗时 |
| 设备预约与队列 | GPU 能力和设备身份进入路由、租约、预约、取消、健康与故障关闭逻辑；CPU/WASI worker 不领取 GPU 任务 |
| 正式结果与可比性 | 正确性和性能分别记录；只比较同题版本、硬件、运行时和基准条件的结果；禁止将 guest 日志当正式性能证据 |
| 页面与学习证据 | 沿用现有 Run/Submit、历史、隐私、进度和分享规则；未验收 GPU 环境时关闭提交 |
| 完整验收 | 参考解、错解、资源超限、恢复、真实队列与浏览器全部实测，记录运行时/策略/设备身份 |

这些实现可以在环境准备期间推进。静态和模拟检查只能验证契约、调度与拒绝行为；真实 GPU 正确性、性能、隔离及回收必须在选定设备上完成。第一道 CUDA 题建议先覆盖简单算子和明确边界，随后再增加优化级别与性能比较。

## 与部署 runner 的关系

GPU worker 不需要注册为 GitHub runner 才能接收学员任务。部署 runner 也不需要 GPU；它只操作预置发布包。将 GPU 隔离测试接入 Actions 时，使用独立、可销毁且不持有生产凭证的验收节点，并仅运行已审查代码。现有 `praximesh-judge` 标签和 `sandbox.yml` 不是 GPU 功能的交付证明。

注册和发布顺序见 [CI 与 runner 指南](ci.md)；完整未完成项见[需求追踪](../requirements.md)。
