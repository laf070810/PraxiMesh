# GPU 评测：现状与接入准备

[部署方案选择](README.md) · **GPU 正式评测尚未交付；本页集中维护宿主 worker 的开发验收配置、环境和组件检查。** 当前 Compose 和发布协调器提供七种 CPU/WASI 评测。添加 `--gpus`、安装 NVIDIA runtime 或注册 Actions runner 都不会自动增加 GPU 题型。

## 本机当前状态

CPU 配额缺口已解决。2026-10-30 在用户 Docker context `/run/user/2031/docker.sock` 实测 CPU、内存、swap 和 PID 控制可用；0.5 核容器运行 5 秒消耗约 2.501 秒 CPU，见 `test-results/cpu-controller-restored/result.json`。最新共用 CPU/runsc 资源与异常回收结果见 [G14](../acceptance-g14.md)。

此前在 GPU 0（RTX 3090）完成了 [G1 的七组正确性检查](../acceptance-g1.md)及 [G2 的九组故障/恢复检查](../acceptance-g2.md)。发现其他账号占用后，维护者要求继续不占 GPU 的开发；新的完整执行路径尚未硬件验收，旧记录不能代替新代码重放。

目前宿主 worker 已连接设备锁、持久预约、私有执行环境、异常回收、二十四阶段启动检查、凭证续检与信号退出，见 [G15](../acceptance-g15.md)。持久成绩、私有学习证据及中英结果展示见 [G16](../acceptance-g16.md)，健康准入、独立额度、队列路由与部署排空见 [G17](../acceptance-g17.md)。[CUDA 编辑器](../editor.md)、[公开 `.cu` 下载与远程提交工具](../local-practice.md)、独立 GPU 秒数筛选和[自愿公开计时榜](../sharing.md)已接入；页面及分享规则检查不使用 GPU。**完整 CUDA 验收、容器化部署、题型发布与真实性能重放仍待完成；设备允许列表默认空，网站的 GPU 提交保持关闭。** 分批历史统一查阅[需求追踪](../requirements.md)和[验收索引](../README.md)。

所有控制器副本和升级必须共用原来的私有工作根与设备锁目录，不能换路径避开旧进程、存储或预约。调度组件使用 `gpu-slot-seconds-v1`：Run 90 秒、Submit 180 秒的整卡时段，默认每日 `JUDGE_DAILY_GPU_SECONDS=1800`，与 CPU 调用单位分开。这些是预约上限，不是测得的 GPU 时间；不能据此启用评测或手工清空预约。

<a id="validation-options"></a>

## 按改动选择检查入口

| 要验证的改动 | 对应入口 |
| --- | --- |
| 用户服务、命名空间、控制连接 | [控制链检查](#不占-gpu-的控制链检查) |
| 固定镜像、隔离和资源限额 | [离线容器检查](#不占-gpu-的离线容器检查) |
| 崩溃后的存储回收、旧预约和就绪事务 | [异常恢复及 PostgreSQL 检查](#不占-gpu-的异常恢复检查) |
| 整控制器退出、通道失联和独立硬期限 | [控制器故障检查](#不占-gpu-的控制器故障检查) |
| 已获授权的空闲单卡与 CUDA 环境 | [单卡环境检查](#重放单卡环境检查)，再按需执行内核和故障检查 |
| 完整启动、长期领取、取消与退出流程 | [宿主 worker](#gpu-worker)，需要独占卡；非 GPU 状态检查使用下方 PostgreSQL 测试 |
| 历史 GPU 成绩、私有进度、中英反馈与布局 | [成绩展示检查](#gpu-results)，不使用 GPU |
| 源码部署的 GPU 准入配置、额度与排空 | [准入门槛](#gpu-admission)，当前只做不占 GPU 的验证 |
| CUDA 候选合并、静态检查和参考解/错解 | [内容准备与验收](#cuda-content)；静态检查不占 GPU，真实 API 验收需要独占卡 |
| 容器监督的独立期限、进程树与接管 | [可信容器监督检查](#container-supervision)；仅固定 CPU 基础设施，不是 GPU 部署入口 |
| 容器内 runsc、会话故障和整树终止 | [嵌套运行时检查](#container-runtime)；固定 CPU guest，尚无 GPU 或正式准入 |
| 容器控制连接、启动身份与断连恢复 | [受信通信检查](#container-transport)；真实 CPU guest，不生成 GPU 凭证 |
| 持久容器身份、创建中断及旧制品清理 | [持久恢复检查](#container-journal)；固定 CPU，不释放数据库 GPU 预约 |
| 容器化控制端的 UID、共用锁与重启接管 | [控制端映射检查](#container-controller)；固定 CPU，不连接应用数据库或 GPU |
| 持久容器记录与数据库预约、锁超时和重试 | [数据库/容器联合检查](#container-database)；一次性 PostgreSQL 与固定 CPU guest，不开放 GPU |
| 固定 CUDA 编译器、候选语法和编译后清理 | [无设备编译检查](#container-compiler)；真实 NVRTC 在 gVisor guest 中运行，不占 GPU、不判题 |
| 任务总预算、CPU/内存/PID/存储上限和故障恢复 | [任务预算检查](#container-workload)；整个任务容器含运行时开销，不等同于 guest 独占资源或 GPU 显存 |

<a id="container-compiler"></a>
<a id="container-workload"></a>

## 不占 GPU 的 CUDA 编译与任务预算检查

适用于准备容器执行镜像、修改 NVRTC 编译逻辑、检查两道 CUDA 候选能否编译或验证任务资源上限。需要同一私有 rootless Docker、固定 runsc 及 NVRTC 离线归档；归档来源见[固定离线制品](#提前保存固定镜像减少下载依赖)。在仓库根目录执行：

```sh
.venv/bin/python scripts/container_compiler_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --resource-profile container-workload-v1 \
  --resource-probes
```

工具构建独立的固定编译镜像，使用真实 gVisor guest 与 NVRTC 13.1.115；不挂载 GPU 或 CUDA 驱动，不连接数据库。编译目标固定为 compute 8.6，这是编译参数，不是硬件检测结果。它检查候选参考实现、六份语义错解、编译错误、非法请求和平台配置错误，并在编译后断连、独立期限及清理之后重新编译正确语法的代码。源码不在宿主或可信外层执行。

上面的模式在 0.5 CPU、512 MiB 宿主内存、无 swap、256 个宿主任务的固定总预算中执行全部编译场景，并增加四项真实资源探针及各自的新容器编译恢复，预期共 26 项。预算包括监督器、runsc 和 guest，不代表 guest 独占这些资源；guest `/tmp` 上限为 32 MiB。内存检查须有内核 OOM 或原容器 OOM 状态证据，PID 检查只运行固定可信的 fork 探针。原开发预算为 `container-supervision-v1`（2 CPU、2 GiB、512 个宿主任务）；去掉最后两个参数可单独重放该预算下的 18 项编译检查。

报告为 `test-results/px-container-compiler-*/result.json`，构建输出为同目录 `build.log`。须检查总 `passed`、逐场景状态、预算标识、原进程树清理、镜像标签和临时目录清理；清理失败时保留 `retained_storage` 指定的原 journal，排查后按同一身份恢复，不能删除未知对象。编译状态 `compiled` 只表示生成 PTX，语义错解也应能编译；本检查不证明入口加载、CUDA 正确性、性能或 GPU 就绪，不发布候选题目。设计见 [ADR-043](../adr/043-device-free-container-compilation.md)和 [ADR-044](../adr/044-container-workload-budget.md)。

<a id="container-database"></a>

## 不占 GPU 的数据库与容器恢复联合检查

适用于修改 worker 的启动、续检、取消和崩溃恢复，以及排查数据库预约与 journal 不一致。使用下方相同的私有 rootless Docker 和固定 runsc 归档：

```sh
.venv/bin/python scripts/container_database_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2
```

工具构建固定任务镜像，再调用现有 PostgreSQL 测试入口创建自有数据库和卷。它只使用合成的隔离预约及固定 CPU guest，在原 v1 开发预算与新 v2 任务预算下分别检查运行/创建中断、保留已消费或未消费额度、删除账号、预约错配、数据库锁超时、归档失败重试、换锁拥有者及归档后的数据库变化；同时检查已有宿主 worker 生命周期。

报告在 `test-results/px-container-database-*/result.json`，SQL 及组件输出在同目录 `postgres.log`/`postgres.xml`。逐场景报告区分正式恢复动作与测试结束时清理自有对象；错配时不能因为测试清理成功就声称生产预约已释放。只有场景、组件、原进程树和测试资源清理全部完成才有 `passed: true`。检查不连接已有站点数据库，不生成 GPU receipt，也不授予设备就绪状态。

恢复设计见 [ADR-042](../adr/042-container-recovery-database-fence.md)。需要宿主 worker 同时处理已记录容器时，按下方[可选恢复配置](#worker-container-recovery)接入；这仍不构成完整 GPU Compose。

<a id="container-controller"></a>

## 不占 GPU 的容器化控制端检查

适用于开发 GPU 容器部署、确认控制端与宿主是否共用同一把锁及恢复记录。它构建固定的可信控制镜像，连接下方检查使用的私有 rootless Docker；需要 Linux x86_64、固定完整 runsc 归档和 Docker CLI 29.9.2 官方归档。CLI 不安装到宿主，只校验并复制进测试镜像：

```sh
mkdir -p var/gpu-preflight
curl --fail --location \
  https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz \
  --output var/gpu-preflight/docker-29.9.2.tgz
.venv/bin/python scripts/container_controller_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --docker-cli-archive var/gpu-preflight/docker-29.9.2.tgz
```

构建工具核对整个 CLI 归档和二进制的固定 SHA-256；源码、基础镜像和运行时也固定身份。控制端只挂本次临时私有目录与精确 socket 文件，不挂整个用户目录，不依赖宿主 Python 或用户服务。镜像内 UID/GID 0 必须实际映射到宿主非 root 操作者；宿主、两个控制容器通过同一锁 inode 竞争。改变 UID、锁目录或 socket 身份应在创建任务前拒绝。

随后运行真实 CPU guest，终止其可信控制容器，由另一容器或宿主读取原 journal 清理，并验证宿主创建的新预算任务（journal v2）可以被容器接管；其余场景继续覆盖原开发预算和 v1 记录。新调用使用新的实例和 nonce。任务容器继续无宿主挂载、无网络、无设备，使用独立期限。控制端拥有所选 daemon 的控制权；只读 socket 挂载不是 API 权限隔离，生产必须使用专用可信执行环境。

结果保存在 `test-results/px-container-controller-*/result.json`；核对每项检查、原进程树消失、任务和控制容器清理、镜像标签及临时存储移除。失败时保留原始报告；无法确认清理时保留恢复目录，不能手动清空记录来绕过身份检查。资源与权限决策见 [ADR-041](../adr/041-containerized-control-identity.md)，初次及当前预算兼容检查分别见 [G25](../acceptance-g25.md)和 [G28](../acceptance-g28.md)。此工具不部署常驻 GPU worker，不连接应用数据库、不生成 receipt；完整 GPU Compose、设备监测和正式容器环境仍待交付。

<a id="container-journal"></a>

## 不占 GPU 的容器持久恢复检查

沿用下方容器检查的 rootless Docker 与固定完整 runsc 归档。存储还需支持原子重命名、文件/目录同步及文件锁；本工具在自己的结果目录验证这些操作，不修改已有站点状态：

```sh
.venv/bin/python scripts/container_journal_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2
```

它依次在意图写入后、创建后但 ID 未记录、ID 已记录、真实 CPU guest 运行中及归档后中断控制器；由新进程读取原记录清理，再通过新调用运行固定 CPU guest。升级场景从源码副本执行改变摘要后的恢复控制器，确认新执行拒绝旧契约，而仅清理路径可以处理原对象；另实测拒绝停止同名替换容器。

记录在 `test-results/px-container-journal-*/result.json`，应检查每项中断位置、原调用清理、归档、新调用及最终 `passed`；各场景的 `recovery.log` 保留恢复子进程输出。持久记录不是启动 receipt，恢复结果为 `ready: false`；后续生产 worker 仍须完成设备和数据库恢复。该入口不部署常驻 worker。设计见 [ADR-040](../adr/040-durable-container-ownership.md)，初次及当前预算兼容检查分别见 [G24](../acceptance-g24.md)和 [G28](../acceptance-g28.md)。

<a id="container-transport"></a>

## 不占 GPU 的容器控制通信检查

使用与下方容器内 runsc 检查相同的 rootless Docker 环境和固定归档：

```sh
.venv/bin/python scripts/container_transport_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2
```

此入口核对固定容器身份及启动握手，再经过真实控制连接驱动固定 CPU guest。检查包括 1 MiB 消息双向传输、guest 不可见通信路径、身份错配、超长帧、断连、客户端退出、取消、额外输入、独立期限及新调用恢复。它不接受任意程序输入，不创建应用数据库，也不会开放 GPU 提交。

报告在 `test-results/px-container-transport-*/result.json`。只有全部检查和自有容器、镜像标签清理成功才有 `passed: true`。断连或 CLI 退出不是清理完成，报告还需有 guest 清理或外层独立停止及进程身份消失的证据。机制与边界见 [ADR-039](../adr/039-container-control-transport.md)，实测结果见 [G23](../acceptance-g23.md)。

<a id="container-runtime"></a>

## 不占 GPU 的容器内 runsc 检查

在具备下方监督检查条件的同一用户环境，使用已下载的固定完整 runsc 归档：

```sh
.venv/bin/python scripts/container_runtime_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2
```

归档准备与摘要见[单卡环境检查](#重放单卡环境检查)的制品说明；此命令本身不读显卡、不调用 CUDA，也不需要编译器归档。它使用自有构建目录、镜像标签、容器和合成锁身份，不导入应用数据库。构建仅传递固定源码契约摘要，客户端不继承代理或应用凭据。

新镜像使用专门审阅的 syscall 配置，让固定 runsc 在可信外层中创建私有命名空间；不修改 daemon，不使用 privileged/unconfined。直接 OCI guest 使用虚拟 UID 0、空能力、只读根、断网和有限临时目录。外层仍为非 root UID 65532，**2 CPU、2 GiB、512 任务是整个监督容器的总上限，不是独立 guest 配额**。

工具检查私有控制文件与网络不可见、输出/时间超限、临时存储、附着客户端退出及每次故障后的新 guest 恢复；另检查可信控制器退出和独立期限下的整棵进程树。结果保存在 `test-results/px-container-runtime-*/result.json`，只有检查和自有资源清理全部成功才标记 `passed: true`。失败报告保留，不表示 GPU 可用。设计及限制见 [ADR-038](../adr/038-nested-runsc-container.md)和 [G22](../acceptance-g22.md)。

<a id="container-supervision"></a>

## 不占 GPU 的可信容器监督检查

此入口验证后续容器部署所需的任务监督基础，尚未连接 GPU worker、设备或提交准入。需要当前用户的 rootless Docker、systemd/cgroup v2 和可用的 CPU、内存、swap、PID 限额。它不安装或修改 daemon，不需要 NVIDIA 工具、GPU 或 privileged 容器。

明确选择自己的私有 Unix socket，在仓库根目录执行：

```sh
.venv/bin/python scripts/container_supervision_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock
```

工具从固定 Python 基础镜像构建独立监督目标，使用空白 Docker 客户端配置和最小环境；不继承代理或应用凭证。首次构建需要能取得固定镜像元数据，构建失败会保留日志，不会启动 GPU 或调整网络配置。

预期 `test-results/px-container-supervision-*/result.json` 包含七个通过项：`deadline-busy`、`parent-exit`、`owner-exit`、`takeover`、`replacement`、`foreign-label`、`late-start`。还应有 `passed: true`、`gpu_executed: false`、八个自有容器清理记录和 `owned_image_tag_removed: true`。检查实际 cgroup 数值、CPU 节流及进程树消失；首次创建者退出后，容器仍须按原截止时间停止。

全部进程只运行固定的可信 CPU 探针。合成设备 UUID 仅命名临时文件锁，不读显卡、不创建正式设备行或凭证。失败时保留私有报告与已知身份；不能删除未知容器、重置 GPU，或把该结果当作学员 guest 隔离通过。设计与范围见 [RFC-021](../rfcs/021-gpu-container-supervision.md)、[G21](../acceptance-g21.md)。

<a id="gpu-admission"></a>

## GPU 准入配置与部署排空

本节对应源码 Web/控制命令的配置。GPU Compose 与发布协调器容器接入尚未交付；不能将此变量直接当成完整 GPU 部署方案。在正式题型和当前完整 CUDA 路径验收完成前，保持 `JUDGE_GPU_DEVICES` 为空。

完成这些验收后，Web 和控制命令可将 `JUDGE_GPU_DEVICES` 设置为逗号分隔的完整设备 UUID，最多十六个，不接受 `0`、`all`、通配符或重复设备。名单只选择容量，不签发凭证、不预约物理设备；worker 仍由其显式 `--gpu` 参数选择单卡并执行完整启动。`JUDGE_ENABLED=1` 是总开关，`JUDGE_DAILY_GPU_SECONDS` 默认 1800，独立于 CPU 调用额度。

迁移 `core.0005_gpu_admission_environment` 为设备增加启动环境，旧记录保持原预约且环境为空。Web 只接受同一 UUID、指纹、当前执行策略和新鲜心跳；忙碌设备还须持有有效原任务租约。新 worker 完成全套检查后在同一事务发布环境与 ready。Web 不需要设备表写权限或私有凭证目录；不要手动填环境 JSON 来绕过启动。代码、环境或所有者变化后须由 worker 重新验证。

`/health/` 的 `evaluation` 仍表示 CPU/WASI，新增 `gpu_evaluation` 独立表示 GPU 是否接受新任务。`judge_admission status` 保留原字段，增加 `gpu_configured`、`gpu_worker_available`、`gpu_reserved` 和 `incompatible_gpu_reservations`。`drain` 同时等待任务和物理预约；超时保持暂停，不清空设备。即使账号或任务被删除、设备移出配置，残留预约仍须由原持锁恢复流程处理。`resume` 要求当前 CPU worker、已配置 GPU 池的健康容量及兼容待处理任务；失效的物理预约阻止恢复。

非 GPU 检查使用 `tests/test_gpu_admission.py`、`tests/test_gpu_admission_settings.py`，实际锁/角色/迁移检查使用下方既有 PostgreSQL 入口执行 `tests/test_gpu_admission_postgres.py`。测试只在自有临时数据库建立合成身份；详见 [G17](../acceptance-g17.md)。

<a id="gpu-results"></a>

## 不占 GPU 的成绩展示检查

在独立开发环境准备浏览器依赖及 Chromium 后执行：

```sh
.venv/bin/python scripts/browser_acceptance.py --gpu-results
```

它通过现有验收入口创建一次性 SQLite、合成账号和回环 Web 服务，关闭评测，只写入明确的合成成绩；不连接 GPU worker，也不创建设备行或运行凭证。真实 Chromium 检查中英、390/768/1440 像素，以及正确 Submit、重复批次失败、公开 Run 和损坏记录共二十四组；另有六组做题页预算/关闭按钮检查，并验证其他账号不能查看私有源码与成绩。结果在 `test-results/px-gpu-results-*/result.json`；须有 `passed: true`、`other_account_denied: true`、`gpu_executed: false`，记录的临时数据库在入口退出后应不存在。

修改结果或预约展示时，同时按开发指南运行 `scripts/browser_acceptance.py --interface`，核对现有编辑器和列表布局。成绩一致性与私有课程/挑战可用 `tests/test_gpu_evidence.py` 在 SQLite 和[临时 PostgreSQL](#不占-gpu-的异常恢复检查)上验证。这些合成计时不是 GPU 性能证据；正式结果仍须来自经过完整启动检查的真实执行路径。

<a id="gpu-worker"></a>

## 宿主 worker：供独占硬件验收

适用于完成节点准备、已获得空闲独占卡、准备重放完整 CUDA 路径的开发维护者。当前阶段尚未执行该命令的完整硬件验收；维护者要求暂不占 GPU 时，继续使用下文 CPU 和数据库检查。此入口不注册网站题型，也不提供 Compose GPU 容器。

使用 Linux 宿主的固定项目 Python 环境和普通用户 systemd 管理器；需要下文列出的 cgroup v2、RootlessKit、Podman/runsc、NVIDIA 依赖与三个[固定离线归档](#提前保存固定镜像减少下载依赖)。连接与应用相同的数据库：源码开发沿用 `SQLITE_PATH`，PostgreSQL 使用专用 worker 角色的 `DATABASE_URL` 和对应 TLS 配置，具体见[生产指南](production.md)。凭据放私密环境中，不写进命令行。所有副本与升级保持同一工作根、锁目录和凭证目录。

先用 `nvidia-smi --query-gpu=index,uuid,name --format=csv` 查到被分配设备的完整 UUID，并设置 `PRAXIMESH_GPU_UUID`。命令拒绝数字索引和 `all`，不因设备重新编号而换卡。示例对应本机 Podman `5.8.2`；其他节点填实际固定版本，并重新完整验收。准备私有目录后，在仓库根目录运行：

```sh
install -d -m 700 var/gpu-worker var/gpu-worker/jobs var/gpu-worker/receipts
JUDGE_ENABLED=1 .venv/bin/python manage.py gpu_worker \
  --gpu "$PRAXIMESH_GPU_UUID" --worker-id gpu-node-0 \
  --runtime-archive "$PWD/var/gpu-preflight/gvisor.tar.bz2" \
  --image-archive "$PWD/var/gpu-preflight/python-image-20261030.oci.tar" \
  --nvrtc-archive "$PWD/var/gpu-preflight/nvrtc-13.1.115.tar.xz" \
  --podman-version 5.8.2 \
  --directory "$PWD/var/gpu-worker/jobs" \
  --lock-directory "/run/user/$(id -u)/praximesh-gpu-locks" \
  --receipt-directory "$PWD/var/gpu-worker/receipts"
```

每次启动先恢复旧预约，然后执行全部二十四阶段，单阶段最多 180 秒、总预算最多 4320 秒。日志依次出现 `gpu_startup_passed stage=...`，全部通过后才出现 `gpu_worker_ready`。没有跳过 GPU 启动检查、导入报告或复用旧进程凭证的参数；Django 自带的 `--skip-checks` 仅影响应用配置检查。`--once` 仍执行完整启动，只尝试领取一次；退出后撤销就绪，不能用于给另一进程预先“认证”。

凭证最长有效 24 小时，空闲时不足完整任务余量会停用并重跑全部检查。文件缺失、时钟回拨、代码/主机/所有者变化会退出；运行中凭证失效丢弃结果并保留预约。取消、删除或租约丢失时，先核对原服务已停止，再独立恢复并核对相同预约，成功后才可领取下一任务。发现外部 GPU 进程只停止自有执行，不终止其他账号的工作。

用前台 Ctrl-C 或向该 worker 进程发送 SIGTERM 停止。处理器只设置标志，常规回调负责清理、撤销本次凭证和停用设备；中断任务的预约保留到下一次完整启动恢复。数据库网络失联时不能保证 Python 立即返回，实际任务服务仍有独立硬期限。不要删除锁文件、手动释放预约或换工作根绕过故障。日志只包含固定阶段、任务 ID 和错误类别，保留最后成功阶段用于定位。

<a id="worker-container-recovery"></a>

### 可选：启动时恢复原容器记录

仅在这张卡已有持久容器 journal、需要由宿主 worker 接管清理时使用。在上方 `gpu_worker` 命令保留所有原参数，并同时追加：

```sh
  --container-journal "$PRAXIMESH_CONTAINER_JOURNAL" \
  --docker-socket /run/user/$(id -u)/docker.sock
```

`PRAXIMESH_CONTAINER_JOURNAL` 必须指向原先初始化的私有目录绝对路径，其中含 `installation.json`；继续使用创建该记录时的设备锁目录和同一私有 daemon。不要新建空目录或使用验收报告副本替代原状态。命令检查这两个参数成对出现，并核对安装身份、持锁设备与原实例。未配置时，宿主 worker 不连接 Docker。

恢复先停止原容器，归档前后再核对数据库的隔离状态、拥有者和原预约。行锁超时也尝试停止已记录实例，但保留活动记录与预约；错配时先查原 journal 和数据库状态，不手工释放或换目录重试。归档失败可在故障排除后通过同一入口重试。清理只返回 `ready: false`，后续独立 GPU 恢复、全部二十四阶段和就绪事务仍必须通过。

这两个参数只接入旧容器清理，当前任务仍由原宿主执行路径处理。它们不把 worker 转为容器执行后端，也不替代完整 GPU Compose。数据库与固定 CPU 容器的验证入口见[联合检查](#container-database)。

<a id="cuda-content"></a>

## CUDA 内容准备与真实验收

两道候选及来源见 [CUDA 内容契约](../cuda-contract.md)。默认目录不导入它们。先生成一个可审阅的完整包；此操作只读内容文件，不连接数据库、不编译源码：

```sh
mkdir -p test-results
.venv/bin/python manage.py content_compose \
  --base content/starter.json --append content/candidates/cuda.json \
  --output test-results/cuda-review.json
.venv/bin/python manage.py content_check test-results/cuda-review.json
```

输出为 56 个知识节点、28 道题，权限为 0600，已存在的输出文件不会覆盖。需要重新生成时选择一个新文件名。可以重复 `--append` 加入其他全新 ID；该命令不用于修改已有版本。包包含参考实现和受保护数据，不要放到网站静态目录。

仅查看候选页面时，可以在新建临时 SQLite 库中导入并关闭评测；不要向正在使用的开发或部署数据库导入示例全量包：

```sh
PRAXIMESH_CUDA_REVIEW_DIR=$(mktemp -d /tmp/praximesh-cuda-review.XXXXXX)
env -u DATABASE_URL SQLITE_PATH="$PRAXIMESH_CUDA_REVIEW_DIR/review.sqlite3" \
  .venv/bin/python manage.py migrate --noinput
env -u DATABASE_URL SQLITE_PATH="$PRAXIMESH_CUDA_REVIEW_DIR/review.sqlite3" \
  .venv/bin/python manage.py content_import test-results/cuda-review.json
env -u DATABASE_URL SQLITE_PATH="$PRAXIMESH_CUDA_REVIEW_DIR/review.sqlite3" JUDGE_ENABLED=0 \
  .venv/bin/python manage.py runserver 127.0.0.1:8019 --noreload
```

此时可阅读题意、编辑和保存草稿，不能执行 GPU 评测。停止该测试 Web 后才能删除自己创建的临时库。生产发布仍按原全量内容审核流程处理，不使用此示例覆盖既有目录。

真实内容验收需要另行满足独占卡及完整 worker 启动条件：使用独立测试站点、合成账号和该站点自己的数据库；Web 与 [宿主 worker](#gpu-worker)连接同一测试库，按[准入配置](#gpu-admission)设置对应设备。不得与生产任务混用。当前仍按维护者要求不占 GPU，以下入口尚未完成真实硬件重放。

候选每题运行一次公开参考 Run、一次正式参考 Submit、三次错解 Submit 和一次恢复参考 Submit，共 12 个串行任务、预约 1980 整卡秒。默认每日 1800 秒不足以完成整套检查；只在该隔离测试站点启动时将 `JUDGE_DAILY_GPU_SECONDS` 设为例如 `3600`，并确保测试账号有足够剩余额度。不会绕过配额，也不自动调整生产设置。等待完整启动完成和 GPU 可用，再创建具有提交、读取结果权限的[测试账号令牌](../local-practice.md)，放在 `PRAXIMESH_API_TOKEN` 环境变量中。

```sh
.venv/bin/python scripts/gpu_content_acceptance.py \
  --package test-results/cuda-review.json \
  --incorrect tests/cuda_incorrect_solutions.json \
  --server http://127.0.0.1:8019 --allow-local-http \
  --isolated-test-site --output test-results/cuda-content-hardware
```

外部测试站点使用 HTTPS，私有 CA 用 `--ca-file` 指定；不支持跳过证书检查、代理转发或重定向。命令会先比对所有题目摘要，正式结果须来自同一运行环境；每项报告保留源码摘要、请求键、任务 ID、公开结果及计时，不保存令牌或源代码。报告目录和文件分别为 0700/0600，不覆盖已有目录。

观察期限默认为每题每次任务 600 秒，可用 `--timeout` 调整；它不终止任务。连接中断、平台错误、额度不足、版本变化或结果不匹配均使报告失败，并停止后续提交。保留报告和测试站点，根据任务 ID（或尚未得到 ID 时的幂等请求键）检查已提交任务；不要换请求键重复发送。确认 worker 已恢复设备、没有未结束任务或物理预约后，再关闭自己的测试服务和数据库。报告是内容验收记录，不是 `JUDGE_RECEIPT`，不能用于绕过完整启动。

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
  tests/test_gpu_worker.py tests/test_gpu_worker_postgres.py \
  tests/test_gpu_readiness.py tests/test_gpu_readiness_postgres.py \
  tests/test_gpu_startup.py tests/test_gpu_execution.py \
  tests/test_gpu_control.py tests/test_gpu_resources.py
```

这组检查会自动清理自有数据库容器和卷，覆盖 worker 循环、真实进程信号和领取/结算竞争；GPU 执行使用明确的测试替身，不能作为真实设备恢复证据。不要直接调用数据库的恢复转换来绕过就绪检查。

## 不占 GPU 的控制器故障检查

适用于修改维护协议的握手、控制器退出、通道失联、服务硬期限或后续新调用恢复。复用上面的离线制品与用户服务依赖，串行运行：

```sh
.venv/bin/python scripts/gpu_lifecycle_acceptance.py \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --image-archive var/gpu-preflight/python-image-20261030.oci.tar
```

它在真实 CPU/runsc 容器存活后，执行与 GPU 控制器共用的固定故障握手和终止观察。退出与硬期限须先独立停止；失联须由外层清理仍存活的服务。随后处理映射存储，以同一持久维护身份的新调用、新容器执行固定 CPU 检查。

结果在 `test-results/px-gpu-lifecycle-*/result.json`，应有三个 `passed: true` 的 `checks`。每项 `fault` 与 `recovery` 的 `service_cleanup.cgroup_empty` 和 `workspace_removed` 均为 `true`，两者 `invocation` 及 `runtime.container_id` 不同；总 `passed` 与 `temporary_directory_removed` 也须为 `true`。此入口不映射 GPU、不调用 CUDA、不生成凭证，不证明正式 GPU 路径的恢复或总时限。

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

采样条件固定为同一输入、每批 16 次、2 批预热和 5 批测量；取控制器发送请求到收到结果的批次墙钟中位数，包含通信、分配及同步成本。它不是纯 CUDA 内核时间。此命令只重放会话组件，不能代替完整启动、当前凭证及正常队列验收；[公开计时榜](../sharing.md)的规则和页面已接入，真实性能仍待完整硬件重放。

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
