# 构建、校验与导入发布制品

发布制品把同一源码快照、锁文件、Web 镜像和 WASI worker 镜像绑定起来，供部署与回滚使用。当前已经实现制品流程及独立的暂停/排空命令；服务激活、迁移协调和自动回滚仍待完成，不能将 `load` 当成上线。设计见 [RFC-004](rfcs/004-release-artifacts.md)和[部署协调指南](deployment-coordination.md)。

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

两个镜像共享固定基础镜像及依赖。默认 Docker target 仍为 Web；独立的 `worker` target 增加带 hash 的 `requirements-judge.lock` 和固定 WASI 制品。解释器摘要位于依赖安装层之后，更新制品不必重新下载应用依赖。镜像使用 UID/GID 65532，不包含运行数据库、receipt 或配置密钥。

发布目录只包含三个文件：

| 文件 | 用途 |
| --- | --- |
| `release.json` | 全部源码摘要、两个不可变 image ID、策略/解释器身份及文件大小与摘要 |
| `source.tar.gz` | 实际参与构建的源码快照，可审查与重建 |
| `images.tar` | 两个镜像及共享层的 Docker archive，避免预设 registry |

构建末尾输出 `RELEASE_SHA256`。在可信发布记录中保存这个值；它是 manifest 的 SHA-256。源码与镜像的身份以摘要为准，当前工具不伪称来源一定是干净 Git commit，也不承诺不同构建机产生逐字节相同镜像。

## 校验和导入

将完整目录和经可信渠道确认的 `RELEASE_SHA256` 交给目标主机。不能仅从陌生下载里重新计算摘要，就声称已验证来源。

```sh
.venv/bin/python scripts/release.py verify \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
.venv/bin/python scripts/release.py load \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
```

`verify` 先检查可信 manifest，再检查文件白名单/大小/摘要、源码档案的路径/类型/内容、Docker archive 中的实际镜像配置及每个 layer 的 diff ID；验证不依赖本机已缓存的镜像层，不会解包到工作区或启动容器。`load` 在验证后导入镜像，并检查实际 image ID、平台、非 root 用户、角色、源码/策略标签及解释器配置。后续部署应使用输出的不可变 image ID。

Web 不含 Wasmtime 或解释器，只记录匹配的制品摘要；worker 包含运行时。worker 的 `wasi_service` 入口要求显式 `JUDGE_ENABLED=1`，先在当前实例运行 21 项真实探针，再启动 worker。失败不发布健康记录。所有 guest 代码仍只在 WASI 内执行；打包成普通容器不改变隔离策略，不引入宿主执行回退。

## 重放镜像验收

```sh
.venv/bin/playwright install chromium
.venv/bin/python scripts/release_acceptance.py \
    --release var/releases/candidate --sha256 "$RELEASE_SHA256"
```

脚本导入刚校验的镜像，在独立临时 PostgreSQL 和 Docker 网络上运行 Web/worker。Web 的 HTTP 端口仅绑定回环；worker 只连接内部数据库网络。两者非 root、只读根、删除 capabilities、有内存/PID 限额、使用 init 回收进程，不挂宿主目录或 Docker socket。数据库使用独立 owner/Web/worker 角色；凭证通过选择性的运行环境传入，不进入命令行或镜像。

检查真实启动探针、镜像内策略一致性、Web 不含运行时、浏览器 Run→错误提交→Accepted→路径/历史→删除、真实任务的暂停/超时保持关闭/排空/恢复，以及错误解释器摘要导致启动拒绝。结束清理容器、网络和显式标记的数据库卷，另查卷是否已消失，才输出 `test-results/release-acceptance.json` 和受限日志。它检查当前协议的打包核心功能；生产设置/TLS 另按 [HTTPS 验收指南](production-acceptance.md)验证。

## 当前发布边界

WASI bundle format 2 使用显式 `x86_64-unknown-linux-gnu` target、关闭 component model，并保留 CPython 与固定 SDK 运行库声明。旧 format 1 bundle 和 receipt 不可复用。目标主机仍须运行实际门槛；不同 CPU/内核兼容性不能只靠架构标签宣称通过。

镜像更新不会自动迁移数据库或导入内容；必须明确安排备份、兼容迁移、内容审核、在途任务与回滚策略。不能让不兼容 worker 同时争抢同一个队列。当前尚无完整激活/回滚协调器、registry/部署目标或托管 CI 运行证据。本项目代码与内容许可、公开实例及运营决策也尚未确认；工具不会推送镜像或创建公网服务。
