# GitHub CI 与部署 runner

[部署方案选择](README.md) · 本页说明远端配置完成后的 CI、发布和 runner 接入。日常 Compose 更新仍按 [Compose 手册](compose.md)执行；这里的部署工作流操作 PostgreSQL/HTTPS 发布协调器。

## 三种执行任务分别放在哪里

| 任务 | 当前入口 | 所需机器与权限 |
| --- | --- | --- |
| 应用检查、浏览器、Compose 与真实 WASI 验收 | [ci.yml](../../.github/workflows/ci.yml)、[wasi.yml](../../.github/workflows/wasi.yml) | GitHub 托管 `ubuntu-24.04`，无需自有 GPU 或生产配置 |
| 发布状态、激活与中断恢复 | [deploy.yml](../../.github/workflows/deploy.yml) | 专用 `self-hosted, linux, x64, praximesh-deploy` runner，访问预装协调器和目标站点配置 |
| 原生代码隔离验收 | [sandbox.yml](../../.github/workflows/sandbox.yml) | 可销毁的 `praximesh-judge` 节点；只运行已审查代码，当前仅为 gVisor Python 适配器检查 |

GPU 评测 worker 是接收学员提交的产品服务，与 GitHub Actions runner 职责不同。当前没有 GPU 评测工作流或 GPU worker；见 [GPU 接入准备](gpu.md)。

远端已配置为 `git@github.com:laf070810/PraxiMesh.git`，默认分支 `main`。2026-10-30 已观察到该分支的[应用检查](https://github.com/laf070810/PraxiMesh/actions/runs/41002028127)和 [WASI 验收](https://github.com/laf070810/PraxiMesh/actions/runs/41002028143)实际获得托管 runner 并运行。当次查询时浏览器 job 成功，其余检查仍在执行；应以最终结论及其 commit 为准。远端存在、作业开始、全部验收通过是不同状态。

## 先选择 runner 的权限边界

当前应用仓库公开。**推荐将部署工作流放在独立的私有运维仓库，仅受信维护者可写；部署 runner 只注册到该仓库。** 保持应用 PR 和构建任务在托管 runner 上。运维仓库使用审查过的 `deploy.yml`，固定控制器与发布包仍留在部署主机。

也可以使用组织级 runner，并在服务端实际限制可访问仓库及选定工作流/分支；先确认组织和套餐支持这些限制。自定义 `praximesh-deploy` 标签只负责路由。`deploy.yml` 的默认分支条件和 `production` environment 只约束这个 job，不能阻止另一个工作流直接使用同一 runner。不要将带生产凭证或共享 Docker socket 的长期 runner 直接暴露给公开仓库的 PR 执行入口。[GitHub runner 访问策略](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)

GitHub 对私有仓库的 environment、分支保护和必需审核人功能有套餐限制；私有仓库不能直接照搬公开仓库的设置。若所选仓库无法满足保护要求，保留主机上的手工协调器入口，先完成应用 CI；不要删掉工作流的保护条件来绕过。参见 [GitHub environment 支持范围](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)。

## 部署主机需要准备什么

按[生产部署手册](production.md)先完成独立站点的初始化、代理与服务监管。部署 runner 不需要 GPU，也不需要构建 CUDA。当前工作流使用固定路径：

| 路径 | 用途 |
| --- | --- |
| `/srv/praximesh/controller/` | 经过审查的控制器源码及固定 `.venv`；runner 不在线更新依赖 |
| `/srv/praximesh/config.json` | 私有配置和凭证，权限 `0600` |
| `/srv/praximesh/state/` | 私有部署记录、锁、代理状态与诊断 |
| `/srv/praximesh/releases/<SHA-256>/` | 已验证的 `release.json`、`source.tar.gz`、`images.tar` |

runner 账户需要读取固定控制器、配置和制品，操作本实例状态目录，并有维护该实例所需的 Docker/代理权限。它不得共享科研任务、学员执行器或其他业务的配置与 Docker daemon。Docker 管理权限属于目标实例的高权限运维能力，普通 PR job 不应获得它。

先在主机上用相同账户检查命令：

```sh
/srv/praximesh/controller/.venv/bin/python \
  /srv/praximesh/controller/scripts/deploy.py \
  --config /srv/praximesh/config.json \
  --state /srv/praximesh/state status
```

命令读取并锁定协调器记录，只输出实例、阶段、当前代和发布摘要；必要时会建立状态目录及锁文件。它不连接数据库、不启动容器、不迁移或切换版本。空状态也可能成功返回，所以必须核对预期实例和部署阶段，并单独检查实际站点健康。

## 注册与首次接单

1. 在选定的运维仓库进入 **Settings → Actions → Runners → New self-hosted runner**，选择 Linux x64。使用 GitHub 页面当次提供的下载地址、摘要和注册命令，不把注册令牌写入仓库、日志或聊天。[官方注册步骤](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
2. 在专用账户/节点安装 runner，配置标签 `praximesh-deploy`，保留默认 `self-hosted`、`linux`、`x64` 标签。注册到选定的运维仓库，保持其写权限仅限受信维护者。根据目标主机的服务管理方式配置常驻运行；注册令牌有效期只有一小时。
3. 配置该仓库默认分支保护以及 `production` environment：允许实际默认分支，按套餐可用能力设置审核要求。工作流使用仓库默认分支表达式，支持 `main`，无需改成 `master`。
4. 将 `PRAXIMESH_DEPLOY_ENABLED=true` 设置为**仓库级 Actions variable**。job 的 `if` 在进入 environment 前判断，因此不要只把它放在 environment variables 中。
5. 在 Actions 手工运行 **Activate a reviewed release**，默认操作 `status`。无需填写发布摘要，不应触发升级。核对 runner 身份、目标实例、阶段及摘要。

runner 只需主动连接 GitHub；无需为 runner 额外开放入站控制端口。服务运行状态显示在线/空闲后才能接单；网络要求见 [GitHub 自托管 runner 说明](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)。

## 发布与恢复

1. 选择受信默认分支上最终通过的 WASI 工作流及确切 commit，取得 `immutable-release` artifact；当前保留七天。PR 制品不能直接作为生产批准依据。
2. 通过受信传输预置三个发布文件到摘要命名目录，使用[发布校验命令](releases.md)核对独立确认的 manifest SHA-256。下载制品本身不等于批准发布。
3. 明确选择 `apply`，输入 64 位小写十六进制摘要。仅首次安装且明确希望导入示例目录时启用 `initial_content`；升级保持关闭。
4. 检查部署 job、协调器阶段、HTTPS 健康和实际评测。超时或中断先查询 `status` 和私有 journal，按[恢复流程](production.md)明确选择 `recover`；不要自动重试另一个版本。

工作流不 checkout、不下载执行代码，不接受任意路径或命令；不会自动将运行中的 Compose 站点转成协调器站点。Compose 的 SQLite 数据也不会随 runner 注册而迁入 PostgreSQL。

## 作业没有启动时

| 表现 | 检查位置 |
| --- | --- |
| `skipped` | 是否从实际默认分支触发；仓库变量是否严格为字符串 `true` |
| 等待审批或分支被拒绝 | `production` environment 的保护、可用套餐和部署分支规则 |
| `Waiting for a runner` | runner 是否在线；四个标签是否齐全；runner 是否属于该仓库或可访问的组 |
| 控制器路径/权限失败 | 同一账户是否能执行上面的主机 `status` 命令；私有文件和固定环境是否准备好 |
| `status` 成功但网站不可用 | 它只读部署记录；继续检查代理、Web、worker、数据库及健康入口 |
| 应用或 WASI job 失败 | 打开失败步骤和报告处理实际错误；添加部署 runner 不会修复托管测试失败 |

runner 清单 API 需要相应 GitHub 权限；未认证查询返回 `401` 不能证明没有 runner。设计和验证范围见 [RFC-017](../rfcs/017-hosted-deployment-runner.md)。
