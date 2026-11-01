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
