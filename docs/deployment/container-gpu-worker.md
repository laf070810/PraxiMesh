# 容器 GPU worker：构建、配置与启动

适用于已经使用 PostgreSQL、准备在 Linux x86_64 节点验收独占单卡 CUDA 评测的维护者。专用 worker 命令、固定镜像和独立 Compose 文件已提供；**完整 GPU 启动、真实队列和发布升级仍待硬件验收**。当前普通单机 Compose 的 SQLite 数据不能直接接入这个 worker。方案比较见[部署首页](README.md)，检查目录见 [GPU 指南](gpu.md#validation-options)。

它复用原有账号、题目版本、GPU 预约和队列。可信控制容器使用数据库网络和私有 Docker socket；学员任务继续在单独的无网络 gVisor guest 中运行，拿不到这些挂载或数据库凭据。初始化、完整二十四阶段、当前私有凭证、领取、取消和隔离停用均由同一生命周期完成。

## 先准备哪些条件

- 现有应用 PostgreSQL、专用 worker 登录角色和可达的数据库网络。角色沿用[生产部署](production.md)的最小权限；不能用数据库所有者、超级用户或 Web 角色代替。检查也会拒绝多余的列权限、授权转授和序列权限。
- 同一普通用户的私有 rootless Docker、cgroup v2/systemd 委派，以及实际 CPU、内存、无 swap、PID 限制。容器控制端为 1 CPU、512 MiB、128 PID；任务总预算另按原契约执行。
- 固定 runsc、NVRTC 和 Docker CLI 归档，来源及摘要见 [GPU 离线制品](gpu.md#提前保存固定镜像减少下载依赖)和[控制镜像](gpu.md#container-controller)。本机 NVIDIA open 驱动及受信系统库目录必须与制品匹配。
- 对真实设备的读取、映射与执行须已有明确授权；开始完整启动前须有独占空闲单卡。只允许非 GPU 开发时，可以运行本文最后的无设备验收，不能执行新安装的 `--initialize` 或启动 `gpu-worker`。
- 所有控制器使用同一持久私有状态和锁目录。原宿主仍有任务或预约时，先由原后端完成恢复；不要另建锁目录规避竞争。

宿主 procfs 必须递归只读且为 `rprivate`。Docker 的递归只读子挂载依赖 Linux 5.12 及以上；启动端仍检查实际挂载，条件不符会拒绝启动。[Docker 说明](https://docs.docker.com/engine/storage/bind-mounts/#recursive-mounts)

## 构建固定镜像，不访问 GPU

调试专用 worker 时使用下方本机构建目录；需要将 Web、CPU worker 和 GPU 镜像作为同一版本保存、传递或核验时，使用[统一四镜像发布包](releases.md#gpu-release)。发布包的无设备导入验收也集中在该指南，当前不自动激活或升级 GPU 服务。

在仓库根目录和独立开发环境操作。输出目录每次使用新名称，构建不会覆盖旧包。下面同时构建可信应用 worker 和单卡任务镜像；省略最后两个归档参数时只构建应用 worker。

```sh
install -d -m 700 var/gpu-worker var/gpu-worker/docker-client
export PRAXIMESH_DOCKER_SOCKET="/run/user/$(id -u)/docker.sock"
export PRAXIMESH_GPU_BUILD="$(pwd)/var/gpu-worker/build-$(date +%Y%m%d-%H%M%S)"
export PRAXIMESH_GPU_DOCKER_CONFIG="$(pwd)/var/gpu-worker/docker-client"

.venv/bin/python scripts/build_container_worker.py \
  --docker-socket "$PRAXIMESH_DOCKER_SOCKET" \
  --docker-cli-archive var/gpu-preflight/docker-29.9.2.tgz \
  --management-library-dir /usr/lib64 \
  --output "$PRAXIMESH_GPU_BUILD" \
  --tag praximesh-gpu-worker-local \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz
```

其他发行版使用实际受信系统库目录。此步骤只读取驱动版本文件及库文件，不查询设备、不初始化 CUDA。依赖按 `requirements.lock` 的版本和哈希安装；构建上下文、日志、镜像 ID、管理制品及任务驱动清单保留在输出目录。Docker 构建仍可能需要网络获取固定基础镜像和 Python 包。

`worker-image.json` 记录应用镜像、源码身份、完整管理清单；同时构建任务时还包含 `task_image` 和 `driver_manifest`。使用不可变 ID 运行，不能将浮动标签写入正式配置。不要提交这些本机制品、数据库配置或运行状态。

## 新安装的私有配置

先在私有位置创建一个 0600 JSON 文件，例如 `var/gpu-worker/database.json`，在本机编辑其中的连接信息：

```json
{
  "url": "postgresql://WORKER_USER:URL_ENCODED_PASSWORD@DATABASE_HOST:5432/APP_DATABASE",
  "sslmode": "verify-full"
}
```

密码中的特殊字符须做 URL 编码，URL 不接受 query 或 fragment。远程数据库使用 `verify-full` 并在状态目录放置 0600 的 `database-ca.pem`；连接会同时验证证书和主机名。`require` 仅要求 TLS；同机隔离网络内未配置 TLS 的自有测试数据库可显式使用 `disable`。不要把密码放在下面的命令参数或环境变量中。

以下初始化会读取指定 GPU 的身份和完整进程列表，但不执行内核。需要已有设备读取授权；发现外部进程时拒绝继续，不停止其他账号的进程。`PRAXIMESH_GPU_STATE` 必须是本次新安装的绝对路径；升级或已有控制器不得使用 `--initialize` 创建另一套状态。

```sh
export PRAXIMESH_GPU_STATE="$(pwd)/var/gpu-worker/control"
export PRAXIMESH_GPU_UUID='GPU-填写已授权设备的完整UUID'
export PRAXIMESH_GPU_TASK_IMAGE="$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_image"])' "$PRAXIMESH_GPU_BUILD/worker-image.json")"

.venv/bin/python scripts/configure_container_worker.py \
  --initialize --gpu "$PRAXIMESH_GPU_UUID" \
  --docker-socket "$PRAXIMESH_DOCKER_SOCKET" \
  --worker-image-manifest "$PRAXIMESH_GPU_BUILD/worker-image.json" \
  --state "$PRAXIMESH_GPU_STATE" --worker-id gpu-node-0 \
  --task-image "$PRAXIMESH_GPU_TASK_IMAGE" \
  --driver-manifest "$PRAXIMESH_GPU_BUILD/task/context/driver/manifest.json" \
  --database-file var/gpu-worker/database.json
```

已有完整 v4 挂载清单但尚无 `worker.json` 时，省略 `--initialize` 及其三个初始化参数。工具使用原锁，原子创建配置，不覆盖已有文件。默认 `enabled=false`，只能做只读预检；只有明确准备开始真实验收时才允许 `enabled=true`。`--enable` 可用于已有执行授权的新安装。

使用 `verify-full` 时，初始化后将可信数据库 CA 写入 `$PRAXIMESH_GPU_STATE/database-ca.pem`，权限 0600。配置工具和 worker 不迁移数据库、不发布题目、不创建账号，也不会把检查结果设为正式成绩。

<a id="coordinator-binding"></a>

## 将原节点绑定到发布协调器

此入口面向准备使用 PostgreSQL/HTTPS [发布协调器](production.md)管理 GPU 的新安装，与上文独立 Compose worker 配置二选一。当前已支持节点认领及中断重试；代际数据库切换、GPU worker 生命周期和真实升级仍在接入，`apply` 继续拒绝四镜像 GPU 包。绑定成功不会开放 GPU 提交。

已有 worker、任务或预约的状态不能使用首次绑定。保留其原目录、锁和数据库，由原后端恢复；不能删除 `worker.json`、活动记录或另建锁目录来通过检查。

尚未初始化的新节点，可在已获设备读取授权、确认独占条件后，只准备原私有身份，暂不写 worker 数据库连接。沿用上文构建产生的 `PRAXIMESH_GPU_BUILD` 和原 rootless socket；这条命令会读取 GPU 元数据，**不属于无设备检查**：

```sh
.venv/bin/python scripts/configure_container_worker.py \
  --initialize --prepare-only \
  --gpu "$PRAXIMESH_GPU_UUID" \
  --docker-socket "$PRAXIMESH_DOCKER_SOCKET" \
  --worker-image-manifest "$PRAXIMESH_GPU_BUILD/worker-image.json" \
  --state "$PRAXIMESH_GPU_STATE"
```

在已初始化站点的私有部署配置中增加以下 `gpu` 对象，填实际绝对路径及规范 UUID（`GPU-` 前缀、其余十六进制字母小写），保留所有原字段和密钥：

```json
"gpu": {
  "state": "/absolute/path/to/original-gpu-state",
  "docker_socket": "/run/user/UID/docker.sock",
  "device": "GPU-填写已授权设备的完整UUID"
}
```

显式使用与 CPU 协调器相同的 daemon、配置及部署状态目录：

```sh
DOCKER_HOST="unix://$PRAXIMESH_DOCKER_SOCKET" \
  .venv/bin/python scripts/deploy.py \
  --config /absolute/path/to/deployment-config.json \
  --state /absolute/path/to/deployment-state attach-gpu
```

`attach-gpu` 本身只核对已有私有文件和 Docker 身份，不查询或映射 GPU，不启动 worker、连接数据库或切换代理。它先保存部署侧意图，再写入原节点的 `deployment-owner.json`，最后确认配置摘要；成功状态为 `gpu_attachment: attached`。不能借此修改数据库密码、网络或其他配置。绑定后独立配置工具拒绝写入该节点。

中途退出时，用完全相同的配置和路径重跑 `attach-gpu`。它只接受原意图、原锁及原安装身份；遇到其他所有权记录、未知文件或已存在的 worker 配置会保留记录并失败。绑定未确认前不执行 `apply`、`recover` 或 `maintain`。常规 `status` 仍遵守配置摘要，不能据此绕过未完成的绑定。不要手工修改摘要或认领记录。

不占 GPU 的绑定验收使用合成元数据、自有临时状态、真实 rootless daemon 和进程锁，四个实际 SIGKILL 位置覆盖意图、临时文件、所有权记录及确认；只允许 Docker `info`，不启动数据库、代理或任何容器：

```sh
.venv/bin/python scripts/deploy_gpu_acceptance.py \
  --docker-socket "$PRAXIMESH_DOCKER_SOCKET" \
  --output "test-results/gpu-attachment-$(date +%Y%m%d-%H%M%S)"
```

输出目录必须是新目录。此检查不执行上面的元数据初始化，也不证明 CUDA、GPU 队列或升级成功。设计见 [RFC-022](../rfcs/022-gpu-release-coordination.md)。

<a id="generation-configuration"></a>

### 协调器管理的代际配置

已绑定的节点要求私有 `deployment-generation.json` 与 `worker.json`、原所有权、原目录/锁和安装身份一致。代际记录还绑定数据库、worker 登录角色、发布包及 worker 镜像契约。命令会在 Django 初始化后、取得原设备锁后分别重读完整快照；配置或记录变化时停止，不进入启动探针或领取任务。

已实现身份校验、配置生成和协调器内部的双文件持久发布。发布必须持原协调器/设备锁，确认实际维护路由、原 worker 已停止且禁止自动重启、任务 journal 已清理，以及旧库与目标库都在本次暂停下排空并无物理预约。部署日志保存原目标；节点上的 `deployment-config-transition.json` 在整个切换中阻止 worker 读取，包括标记写到一半的情况。中断只可继续原意图，未知内容保留并失败。

这仍是开发中的内部流程，尚无完成 GPU 生命周期的发布命令；内部容器控制组件见[下节](#worker-lifecycle)。不要手工创建代际记录、移除发布中标记、修改摘要或对已绑定节点套用独立 Compose 的启用步骤。`apply` 继续拒绝 GPU 发布。旧独立 worker 配置保持原行为；旧四镜像包仍可核验，内部发布只接受显式声明 `worker_configuration: generation-pair-v1` 的新制品。

文件/配置与启动边界的无设备组件检查可运行：

```sh
.venv/bin/pytest -q tests/test_container_deployment_config.py \
  tests/test_container_worker_config.py tests/test_container_worker.py \
  tests/test_deploy_gpu.py tests/test_deploy_gpu_config.py \
  tests/test_deploy_gpu_config_database.py
```

上述检查使用实际临时文件、文件锁及 CPU 子进程中断，数据库、代理和容器状态使用显式替身。真实 PostgreSQL 行锁、暂停归属和孤立预约检查使用一次性数据库入口：

```sh
.venv/bin/python scripts/postgres_tests.py -q tests/test_deploy_gpu_config_postgres.py
```

当前实际范围见 [G45](../acceptance-g45.md) 与 [G46](../acceptance-g46.md)。跳过的 PostgreSQL 检查不算通过，文件中断结果也不能替代真实镜像、GPU 或完整部署验收。新协议已纳入 GPU/worker 源码契约，使用时必须构建并验证新镜像，不复用旧 GPU 凭证。

<a id="worker-lifecycle"></a>
### 协调器内部的容器生命周期

`scripts/deploy_gpu_worker.py` 提供固定预检和 worker 的创建、启动、停止与原意图重试，尚未接入完整 GPU 发布命令。预检只读挂载私有配置并连接内部数据库网络；正式 worker 使用原状态、socket、递归只读 procfs 和所选单卡管理映射。凭证不进入参数或容器环境，Docker 客户端使用临时空配置。

创建前保存意图，核验完整运行契约后登记 ID，再允许启动。每次新启动要求本次操作的预检，且预检开始不早于上一 worker 的实际退出时间；记录启动意图后释放设备锁，由 worker 取得原锁。同一启动实例被暂停或重启时不算成功。停止只操作原 ID、契约与启动时间，不抢 worker 的设备锁、不持队列行锁等待、不清空任务或预约。原对象丢失、未知同名对象和未确认的生命周期都须保留现场，不能以删除日志或手工运行 Docker 来绕过。

不访问 Docker 或设备的组件检查：

```sh
.venv/bin/pytest -q tests/test_deploy_gpu_worker.py \
  tests/test_deploy_gpu_config.py tests/test_deploy_gpu_config_database.py
```

此入口使用真实私有文件、文件锁和协调器 CPU 子进程，Docker、数据库及代理为显式测试模型。真实 PostgreSQL 使用上节的一次性数据库命令；实际 Docker 契约、镜像入口、完整 GPU 启动、清理、队列及部署恢复仍待验收，不能从模型通过推断已可运行。当前证据和跳过项见 [G47](../acceptance-g47.md)，设计约束见 [ADR-063](../adr/063-durable-gpu-worker-lifecycle.md)。

<a id="readiness-observation"></a>
### 协调器内部的就绪观察

`scripts/deploy_gpu_readiness.py` 将原运行实例、配置、冻结发布环境及数据库状态一起核对。固定 runtime、runsc、NVRTC 和 Docker CLI 声明从已验证的归档字节读取，不执行归档 Python。设备须属于本代 worker，完整启动后发布的心跳仍新鲜，全部待执行任务和物理预约均已排空；同名旧启动或不同环境不能通过。

每次快照使用单条只读 SQL，环境检查后重新观察容器、心跳与数据库。等待从原容器启动时间计算完整预算，重试不会延长期限；超时保留原容器及任务记录。该组件不缓存成功为曝光凭据，不生成 receipt，也尚未接通 GPU `apply`。

不调用实际 Docker 或设备的检查：

```sh
.venv/bin/pytest -q tests/test_deploy_gpu_readiness.py \
  tests/test_deploy_gpu_readiness_database.py
```

测试使用真实临时文件/源码归档和明确的 Docker、数据库模型，不能证明实际 worker 已就绪。数据库只读事务、其他设备孤立预约及心跳不被阻塞的检查已纳入 `tests/test_deploy_gpu_config_postgres.py`，执行入口见上节；具体结果和未执行项见 [G48](../acceptance-g48.md)。

<a id="worker-recovery"></a>
### 协调器内部的原库恢复

`scripts/deploy_gpu_recovery.py` 将原实例停止、新预检、同一容器启动及完整就绪观察串联为持久流程。输入必须匹配当前私有配置和原数据库；不能先改配置或冻结任务所属库，再去另一个库恢复。曝光意图存在时，只恢复已曝光的当前代。

中断后沿原操作继续。等待中的实例保留原启动期限；已确认退出的本次启动可以在下一次恢复调用中开始新尝试，保留前次记录。最多十六次 worker 尝试，每次最多十六个失败预检，达到边界会保留现场并停止自动处理。成功仍须重新观察；未完成记录阻止配置发布，任务和物理预约只由原 worker 的真实清理协议处理。

不占 GPU 的组件及协调器进程中断检查：

```sh
.venv/bin/pytest -q tests/test_deploy_gpu_recovery.py \
  tests/test_deploy_gpu_worker.py tests/test_deploy_gpu_config.py
```

文件、锁和被终止的协调器进程是真实的，Docker、数据库及设备就绪是显式模型，范围见 [G49](../acceptance-g49.md)。这仍是开发中的内部入口，没有可用的完整 GPU 发布/回滚命令。公共 CPU 发布与恢复会拒绝已有 GPU 配置、容器或恢复状态，包括 GPU→CPU；不能删除这些记录来走 CPU 恢复路径。完整 Docker/PostgreSQL、GPU 启动、队列及整站恢复尚待实测。

## Compose 预检与运行

准备非敏感连接参数。数据库网络须是现有 PostgreSQL 容器所在网络；这里不会新建或改写应用数据库。`PRAXIMESH_GPU_MINOR` 从挂载清单读取物理 minor，不能用 CUDA 逻辑序号代替。

```sh
export PRAXIMESH_GPU_WORKER_IMAGE="$(cat "$PRAXIMESH_GPU_BUILD/image-id")"
export PRAXIMESH_GPU_MINOR="$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["device_observer"]["minor"])' "$PRAXIMESH_GPU_STATE/mounts.json")"
export PRAXIMESH_GPU_DB_NETWORK='填写已有数据库网络名'

docker --config "$PRAXIMESH_GPU_DOCKER_CONFIG" --host "unix://$PRAXIMESH_DOCKER_SOCKET" \
  compose -f deploy/compose.gpu-worker.yaml --profile check run --rm gpu-check
```

`gpu-check` 只挂载只读私有配置并连接数据库，不映射 GPU、Docker socket 或宿主 procfs。成功输出 `Worker image and PostgreSQL checks passed; GPU readiness was not checked.`，只证明镜像/配置、连接、当前表列和最小权限通过，不能用作 GPU receipt。

完成预检、确认独占条件及执行授权后，停止旧控制器，在本机私有 `worker.json` 中将 `enabled` 设为 `true`，再启动：

```sh
docker --config "$PRAXIMESH_GPU_DOCKER_CONFIG" --host "unix://$PRAXIMESH_DOCKER_SOCKET" \
  compose -f deploy/compose.gpu-worker.yaml --profile gpu up -d gpu-worker
docker --config "$PRAXIMESH_GPU_DOCKER_CONFIG" --host "unix://$PRAXIMESH_DOCKER_SOCKET" \
  compose -f deploy/compose.gpu-worker.yaml logs --tail 100 gpu-worker
```

该 profile 才映射所选卡、管理节点、原 socket 和递归只读宿主 procfs。正常入口为镜像内 `container_gpu_worker` 命令，没有跳过完整启动、导入凭证或模拟 GPU 的选项。日志依次记录 `gpu_startup_passed`，完整通过后才有 `gpu_worker_ready`；任何阶段失败都不开放就绪。Web 还须连接同一数据库、使用同一执行策略并明确配置 `JUDGE_GPU_DEVICES`，候选题目仍须经过真实参考解/错解验收后发布。

停止使用相同 Compose 文件执行 `stop gpu-worker`。信号处理保留原锁直到清理和隔离停用完成；状态目录是持久 bind mount，不随容器删除。配置变化需重建容器，单纯 `restart` 不重新读取 Compose 参数；程序每次启动都会重新读取私有配置并执行完整启动。

升级时保留原 locks、journal、安装身份和未完成预约，不能复制旧 receipt、删除活动记录或原地改写旧制品。专用升级/发布协调和故障恢复的部署验收仍在继续；不要把本页首次启动入口当作已完成升级验收。

## 开发时不占 GPU 的验证

```sh
.venv/bin/python scripts/postgres_tests.py -q \
  tests/test_container_worker_config.py tests/test_container_worker.py \
  tests/test_container_worker_database.py tests/test_container_readiness.py
.venv/bin/python scripts/container_worker_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --docker-cli-archive var/gpu-preflight/docker-29.9.2.tgz \
  --management-library-dir /usr/lib64
```

第一个入口使用真实 PostgreSQL 和明确的 GPU 运行时替身验证生命周期。第二个构建实际镜像，使用自有数据库、角色和 Compose 项目检查无设备预检、权限拒绝、配置保护及重建；不查询或映射 GPU，不启动常驻 worker，不签发 GPU 凭证。报告在 `test-results/px-container-worker-*/`，检查结束后核对自有容器、网络、卷、密钥和镜像标签清理。当前结果与未完成项见 [G41 验收](../acceptance-g41.md)。
