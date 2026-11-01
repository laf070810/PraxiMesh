# 构建、校验与导入发布制品

[部署方案选择](README.md) · 供制作或核验生产发布包的维护者使用。Compose 用户无需先执行这些步骤。

发布制品把同一源码快照、锁文件、Web 镜像和 WASI worker 镜像绑定起来。制品流程与单主机发布协调器均已实现：`load` 负责导入，`deploy.py apply` 负责维护窗口内的备份恢复、迁移、候选验证和代理激活。设计见 [RFC-004](../rfcs/004-release-artifacts.md)、[ADR-008](../adr/008-deployment-generations.md)及[部署协调指南](production.md)。

默认 format 1 服务于上述 CPU 部署。开发中的 GPU 发布使用[下方 format 2](#gpu-release)统一保存四个镜像；其制品构建/导入与 GPU 服务激活分别验收，当前协调器拒绝尚未接入的 GPU 激活。

## 构建

在可信 Linux x86_64 工作区完成质量检查，按 [WASI 指南](wasi.md)构建当前策略的解释器，然后执行：

```sh
uv sync --frozen --group judge --group browser
.venv/bin/python scripts/release.py build \
    --bundle "$JUDGE_WASI_BUNDLE" \
    --bundle-sha256 "$JUDGE_WASI_SHA256" \
    --output var/releases/candidate
```

输出目录必须不存在。工具复制允许的源码目录与文件，排除运行状态、环境凭证、私钥、数据库和缓存，拒绝符号链接；从捕获的字节构建，不让工作区后续修改影响正在构建的版本。这不是通用秘密扫描器：维护者仍须审查源码和内容，不能把凭证写进正常代码文件。

仓库存在 `AGENTS.md` 时，源码快照同时保留这份开发指南。旧发布包可以没有它；新增指南不改变旧清单的必填文件或运行策略。

两个镜像共享固定基础镜像及依赖。默认 Docker target 仍为 Web；独立的 `worker` target 增加带 hash 的 `requirements-judge.lock` 和固定 WASI 制品。解释器摘要位于依赖安装层之后，更新制品不必重新下载应用依赖。镜像使用 UID/GID 65532，不包含运行数据库、receipt 或配置密钥。

两种格式的发布目录都只包含三个文件：

| 文件 | 用途 |
| --- | --- |
| `release.json` | 全部源码摘要、不可变 image ID、策略/解释器身份及文件大小与摘要；format 2 另含 GPU 契约和固定驱动/管理清单 |
| `source.tar.gz` | 实际参与构建的源码快照，可审查与重建 |
| `images.tar` | format 1 的两个镜像或 format 2 的四个镜像及共享层，避免预设 registry |

构建末尾输出 `RELEASE_SHA256`。在可信发布记录中保存这个值；它是 manifest 的 SHA-256。源码与镜像的身份以摘要为准，当前工具不伪称来源一定是干净 Git commit，也不承诺不同构建机产生逐字节相同镜像。

## 校验和导入

将完整目录和经可信渠道确认的 `RELEASE_SHA256` 交给目标主机。不能仅从陌生下载里重新计算摘要，就声称已验证来源。以下命令在预先安装、经过审查的控制器源码根目录运行；控制器依赖可用 `uv sync --frozen --no-dev` 安装。三个发布文件不包含控制器虚拟环境。目标主机的控制器应与待部署版本匹配，升级时尤其需要包含新表权限规则。

```sh
.venv/bin/python scripts/release.py verify \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
.venv/bin/python scripts/release.py load \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
```

`verify` 先检查可信 manifest，再检查文件白名单/大小/摘要、源码档案的路径/类型/内容、Docker archive 中的实际镜像配置及每个 layer 的 diff ID；验证不依赖本机已缓存的镜像层，不会解包到工作区或启动容器。`load` 在验证后导入镜像，并检查实际 image ID、平台、角色、用户、源码/策略标签及解释器配置。format 2 还在导入前检查 GPU 角色与入口，拒绝覆盖可变镜像标签；其中可信 GPU worker 为原 rootless 映射的 UID 0，其余三个角色为 UID/GID 65532。后续部署应使用输出的不可变 image ID。

Web 不含 Wasmtime 或解释器，只记录匹配的制品摘要；worker 包含运行时。worker 的 `wasi_service` 入口要求显式 `JUDGE_ENABLED=1`，先在当前实例运行完整策略的真实探针，再启动 worker；E/F 策略为 129 项。失败不发布健康记录。所有 guest 代码仍只在 WASI 内执行；打包成普通容器不改变隔离策略，不引入宿主执行回退。

## 重放镜像验收

本节在独立验收环境运行。除了镜像内的 worker，它还在宿主执行公开任务的本地 CLI 测试，因此需要 browser/judge 依赖及与该 release 匹配的已验证 WASI bundle。先按 [WASI 指南](wasi.md)准备本地 bundle，设置 `JUDGE_BACKEND=wasi`、绝对路径 `JUDGE_WASI_BUNDLE` 及经可信构建记录确认的 `JUDGE_WASI_SHA256`；后者必须等于 release manifest 的 `judge.bundle_sha256`。仅执行 `verify`、`load` 或协调器部署不需要宿主安装 Wasmtime 或单独提供 bundle。不要在运行中的服务环境执行以下依赖同步。

```sh
uv sync --frozen --group judge --group browser
.venv/bin/playwright install chromium
.venv/bin/python scripts/release_acceptance.py \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
```

脚本导入刚校验的镜像，在独立临时 PostgreSQL 和 Docker 网络上运行 Web/worker。Web 的 HTTP 端口仅绑定回环；worker 只连接内部数据库网络。两者非 root、只读根、删除 capabilities、有内存/PID 限额、使用 init 回收进程，不挂宿主目录或 Docker socket。数据库使用独立 owner/Web/worker 角色；凭证通过选择性的运行环境传入，不进入命令行或镜像。

检查真实启动探针、镜像内策略一致性、Web 不含运行时、浏览器 Run→错误提交→Accepted→路径/历史→删除、真实任务的暂停/超时保持关闭/排空/恢复，以及错误解释器摘要导致启动拒绝。结束清理容器、网络和显式标记的数据库卷，另查卷是否已消失，才输出 `test-results/release-acceptance.json` 和受限日志。它检查当前协议的打包核心功能；生产设置/TLS 另按 [HTTPS 验收指南](validation.md)验证。

## 激活与恢复

发布包通过核验后，按[生产部署手册](production.md)准备私有配置、PostgreSQL、Caddy 和服务监管，再执行 `deploy.py apply`。首次导入示例内容才使用 `--initial-content`，升级保持原目录。

排空、维护窗口、候选验证及开放前后不同的恢复规则统一在该手册说明。真实部署与迁移结果见[验收索引](../README.md)，构建和导入成功只证明制品环节完成。

## 当前发布边界

更新依赖或基础镜像时，在独立构建环境调整锁文件/固定摘要，完成相关测试与依赖审计，再构建新的发布包。依赖更新可用 `uv lock --upgrade-package PACKAGE`、`uv sync --frozen`；同时按 CI 中的导出命令更新 `requirements.lock` 和 `requirements-judge.lock`。记录更新前后镜像摘要及验收，不在运行服务的虚拟环境中同步依赖，也不原地修改已有发布包。

当前 WASI bundle format 3 使用显式 `x86_64-unknown-linux-gnu` target、关闭 component model，绑定 Python、QuickJS、桥接代码与兼容补丁，并保留两种解释器和固定 SDK 运行库声明。旧 format 1/2 及旧 receipt 不能直接供当前 worker 使用；冻结旧版镜像仍按各自历史契约验收。目标主机须运行实际门槛；不同 CPU/内核兼容性不能只靠架构标签宣称通过。

单独导入镜像不会迁移数据库或切换服务；激活必须通过已配置的协调器，并审查内容与迁移。部署工作流只支持手工触发，默认禁用；GitHub 托管 CI 已开始运行，最终结论及受保护环境、专用 runner 和目标站点的验收需按 [CI 指南](ci.md)核对。本地演练不证明公有证书、系统服务、持续容量或异机灾难恢复已通过。本项目代码与内容许可、公开实例及运营决策仍待确认；工具不会推送镜像或自行创建公网服务。

<a id="gpu-release"></a>

## 同时制作 GPU 镜像的发布包

适合准备容器 GPU 节点及后续协调升级的维护者。先准备与 [GPU worker 构建](container-gpu-worker.md#构建固定镜像不访问-gpu)相同的固定归档和受信库目录。五个 GPU 参数必须一并提供，CPU 与 GPU 构建须指向同一个已验证的私有 rootless engine；此过程不访问设备，不需要选择 UUID。

```sh
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
.venv/bin/python scripts/release.py build \
  --bundle "$JUDGE_WASI_BUNDLE" --bundle-sha256 "$JUDGE_WASI_SHA256" \
  --output var/releases/gpu-candidate \
  --gpu-docker-socket "/run/user/$(id -u)/docker.sock" \
  --gpu-docker-cli-archive var/gpu-preflight/docker-29.9.2.tgz \
  --gpu-runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --gpu-nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --gpu-management-library-dir /usr/lib64
```

该 format 2 保存同一冻结源码的四个角色：`web`、`worker`、`gpu_worker`、`gpu_task`。GPU 有序源码范围和契约来自冻结构建器；校验时从归档重新计算，不用当前源码代替旧版本，也不执行归档代码。数据库凭据、GPU UUID、私有状态与运行凭证都留在部署节点，不进入发布包。构建使用临时镜像标签，导入前按上面的 `verify` / `load` 命令核对独立可信摘要。

导入后的无设备应用检查使用新的独立数据库和 Compose 项目：

```sh
.venv/bin/python scripts/container_worker_acceptance.py \
  --docker-socket "/run/user/$(id -u)/docker.sock" \
  --release var/releases/gpu-candidate --sha256 "$RELEASE_SHA256"
.venv/bin/python scripts/release_acceptance.py \
  --release var/releases/gpu-candidate --sha256 "$RELEASE_SHA256" \
  --output-directory test-results/gpu-release-acceptance
```

第一个入口核对源码匹配、真实数据库最小权限、私有配置、默认关闭及失败拒绝，不映射 GPU、发布 ready 或执行任务。第二个入口沿用上文的浏览器/judge 依赖及匹配的本地 WASI 配置，验证包内 CPU 应用；输出目录必须是新目录。两者都不能作为 GPU 硬件验收。

当前 `deploy.py apply` 会在任何服务/数据库变更前拒绝 format 2，直到 GPU 排空、停止、原状态保留与候选完整启动接入协调器。首次独立 worker 配置仍按 [GPU 容器指南](container-gpu-worker.md)操作；不要将制品校验成功解释为升级成功。设计与实际结果见 [ADR-058](../adr/058-gpu-release-artifacts.md)和 [G42](../acceptance-g42.md)。
