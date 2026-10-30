# GPU 评测：现状与接入准备

[部署方案选择](README.md) · **GPU 正式评测尚未交付；本页提供环境和内核组件检查，不是 GPU worker 的部署入口。** 当前 Compose 和发布协调器提供七种 CPU/WASI 评测。添加 `--gpus`、安装 NVIDIA runtime 或注册 Actions runner 都不会自动增加 GPU 题型。

## 本机当前状态

2026-10-30 维护者补齐 CPU 委派并重启用户 Docker 后，当前 context `/run/user/2031/docker.sock` 的 `MemoryLimit`、`SwapLimit`、`PidsLimit`、`CpuCfsQuota` 均为 `true`，资源控制警告为空。可信临时容器设置 `--cpus=0.5`，实际运行 5 秒消耗约 2.501 秒 CPU，`cpu.max=50000 100000`，发生 50 次节流；测试容器已清理。CPU 配额缺口已解决，结果见 `test-results/cpu-controller-restored/result.json`。

维护者明确指定第 0 张 RTX 3090 供独占验收，按 UUID 选择设备。固定 gVisor `release-20261026.0` 与驱动 `590.48.01` 的实际组合已跑通：在独立用户 cgroup 命名空间、Podman、runsc 与 CDI 中，只识别目标卡，并完成 50,000 元素 CUDA 向量加法。最终 CPU 探针用时 9.002 秒、消耗 4.393 秒 CPU，发生 75 次节流，0.5 核限额生效。临时资源已清理，原站点正常。

初次接入失败和修正见 [RFC-018](../rfcs/018-gpu-environment-probe.md#实际结果与下一步)。之后实现了有界 CUDA 内核契约和受信驱动，真实向量/矩阵参考解、三种错解、编译错误及缺失入口共 7 组检查通过，见 [G1 组件验证](../acceptance-g1.md)。GPU 正式题型、worker、设备预约、显存与故障处理、性能比较仍未交付。

## 重放单卡环境检查

适用于准备 GPU 执行环境和排障；它不安装常驻 worker、不生成评测 receipt，也不会开放网站提交。需要维护者明确分配一张空闲 GPU。脚本拒绝 `all`，发现已有计算任务就退出。

本机已具备：普通用户可用的 systemd 管理器及委派的 cgroup v2 控制器、RootlessKit 3.3、Podman 5.8、slirp4netns、fuse-overlayfs、NVIDIA Container Toolkit 和受固定 runsc 支持的驱动。使用项目 Python 3.11+ 环境；不要使用 sudo。其他机器先准备这些依赖与权限，并允许拉取固定的 NVIDIA 样例镜像。

本机归档已在 `var/gpu-preflight/gvisor.tar.bz2`，可直接执行最后一条命令。新环境先下载固定完整归档，脚本会核对内置 SHA-512 并保留 sidecar 二进制：

```sh
mkdir -p var/gpu-preflight
curl --fail --location --retry 3 \
  --output var/gpu-preflight/gvisor.tar.bz2 \
  https://storage.googleapis.com/gvisor/releases/release/20261026.0/x86_64/gvisor.tar.bz2

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

两种模式串行执行，不接受任意学员源码、不生成正式 receipt，也不开放网站提交。正常内核成功不能代替非法访存、显存耗尽、超时、设备恢复和可信性能验收。实现与未完成门槛见 [RFC-019](../rfcs/019-gpu-kernel-evaluation.md)和 [G1 记录](../acceptance-g1.md)。

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
