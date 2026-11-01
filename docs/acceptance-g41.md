# G41：容器 worker 命令、镜像与数据库部署

2026-11-01。专用入口和部署配置已实现，本轮非 GPU 验证完成。设计见 [ADR-057](adr/057-container-worker-deployment.md)，操作集中在[容器 worker 部署](deployment/container-gpu-worker.md)。本轮未查询、映射或占用 GPU，也未更新现有运行站点。

## 当前实现

专用 Django 命令复用共同完整启动、领取、取消、当前凭证和隔离退出。应用镜像加入锁定 Django/psycopg、固定 CLI、管理制品和完整源码校验；原 CPU 控制夹具保留。配置工具使用原私有锁和原子创建，默认关闭执行。独立 Compose 预检不映射设备/socket/procfs；正常 GPU profile 才挂载原控制状态并启动完整检查。

数据库预检使用实际 PostgreSQL 只读事务和原 worker 授权，拒绝超级用户、所有者、多余表/列/转授/序列权限及结构错配。凭据来自私有配置，不进入启动参数、Compose 展开结果或错误日志。没有迁移、题目发布或运行凭证的兼容捷径。

## 验证记录

首轮配置、命令生命周期和原控制器组件 **84 项通过**，见 `test-results/g41-components-first.log` 和 `.xml`。初次 Ruff 导入提示已修正。

实际 PostgreSQL 首轮 **134 passed、1 failed**，见 `g41-postgres-first.log` 和 `.xml`。通过范围包括命令启动/正式任务/取消/停止、原宿主 worker、完整容器检查、发布竞争、只读 Web 准入及最小权限。唯一失败为新安装测试使用的引擎替身未提供清单所需的 socket 文件身份；补齐夹具后，两项初始化专项通过，见 `g41-initialization-fixed.log` 和 `.xml`。原失败保留，未将首轮描述为一次全绿。该入口确认自有 PostgreSQL 容器和卷已清理。

命令测试的 GPU 方法是明确替身，不能证明 CUDA 成功；初始化专项也替换设备查询，只核对原文件锁和清单，不实际查询 GPU。

最终配置、命令与实际 PostgreSQL 检查 **53 passed、0 skipped**，见 `g41-postgres-final.log` 和 `.xml`。其中两项在真实最小权限 worker 角色下运行完整启动、正式领取/结算及取消，GPU 方法仍是明确替身。原适配器、控制器、journal、执行、凭证、成绩、启动、准入与发布兼容回归 **283 passed、0 skipped**，见 `g41-compatibility.log` 和 `.xml`。这些批次存在范围重叠，不与首轮相加为独立测试总数。

实际应用镜像/Compose/PostgreSQL 最终 **八项通过**，见 `test-results/px-container-worker-69037cde4744/result.json` 及 `g41-worker-image-final.log`：

- 最小权限连接与重建；继承的错误数据库/启用环境不会覆盖私有配置。
- 默认关闭执行、0644 配置、多余用户密码列权限和被改写的源码分别在预期边界被拒绝；恢复后检查再次通过。
- 使用实际生产入口和旧 v1 无设备控制清单时，先通过真实数据库及控制器映射、cgroup、CLI 检查，再拒绝旧清单；原锁可重新取得，安装身份不变，没有任务、设备发布或运行凭证。

前两次真实镜像运行分别因测试数据库只有内部网络、验收脚本未显式展开 Compose profiles 失败，均发生在检查用例之前；没有修改产品隔离要求。数据库改为自有管理/内部双网络，配置检查使用 `--profile "*"` 后重放七项通过，再增加旧控制清单和明确拒绝阶段检查，完成最终八项。原报告保留在 `px-container-worker-f9f5311401a8`、`px-container-worker-19c8958463af` 和 `px-container-worker-99f005937e0f`；各轮自有容器、网络、卷和镜像标签均由入口清理。

原 CPU 控制器 **七项实际场景通过**，见 `test-results/px-container-controller-b30471cd2012/result.json` 和 `g41-controller.log`。包含双向文件锁、UID 映射、错误用户/锁/socket 拒绝，以及容器到容器、容器到宿主、宿主到容器的原记录恢复。十五个自有容器和临时状态已清理；没有启用管理设备观察。

最终 worker 镜像为 `sha256:370773d83713d4eeaea215c43cd58e34528bb9a94d7278f03c9febc6d44ba5fd`，应用源码契约为 `c87aef559e2ff414e43306968a1a9bda237415999f586ec8279bb7cda90f48b8`，控制器契约为 `ff3c1708a168544a0ac6b11b27a3968874d81ce83398619ae2fa478020fddd58`。冻结源码保留在相应 `worker/context/source` 中；管理制品来自本机受信文件快照，没有调用设备接口。

文档中的联合构建 CLI 也已实际通过，记录为 `test-results/px-g41-pair-99c73b5442cc/result.json` 和 `g41-pair-build.log`。它重新得到同一 worker 镜像，并生成配套任务镜像 `sha256:95997fcca98f1a82baf748ce4d72d1e86f0c68af3080e656654a1631df5f7fa9`，任务契约为 `aa67185c3e70828fd51205b144ce94b18b32cac6f870d7bbc0638f9efe64b2a8`。汇总清单、实际不可变 ID、驱动清单、管理版本及镜像标签相互匹配；两个自有标签已清理。该步骤只构建静态制品，没有创建带设备的容器或启动任务。

## Web 镜像与默认 Compose

部署闭环检查发现 Web 基础镜像未携带 `EXECUTION_SOURCES` 中的四个脚本，使 GPU 准入无法计算当前执行摘要。补齐精确文件复制，并在构建时执行纯源码摘要检查；默认 Compose 验收也在首次启动和重建后核对 Web/CPU worker 的完整摘要。该检查只读取文件，不访问 GPU。

修复前后分别使用冻结源码构建实际 `web` 镜像，在无网络、无挂载、无设备的只读容器中验证。旧镜像明确因 `/app/scripts/container_worker_entry.py` 缺失退出 1；修复后的镜像退出 0，完整摘要与冻结源码及当前源码一致，为 `ef7a1d98948f186849b4d0823d30bf5a6206456be58796239f08ef8daf1111a5`。两轮记录及清理结果在 `test-results/g41-web-package/{before,after}/result.json`，修复后镜像为 `sha256:650303cfec07d2af96241114ea577c1383d403de914d92a033fb920b69583c42`。

默认 Compose 的实际镜像验收通过，见 `test-results/px-compose-4a6a6e34508b/result.json` 和 `g41-compose.log`：129 个真实 WASI 探针、七类正式提交、一个错解和一个公开 Run、worker 离线关闭提交、直接 IP/CSRF、编辑器静态制品，以及重建后的账号/草稿/成绩/会话/自定义目录/撤回保留均通过。首次启动和重建后的四次 Web/CPU worker 摘要检查全部一致，自有容器、网络、卷和镜像标签已清理。本轮没有启动 AI 或重放浏览器操作，不将此前界面证据记为本次重新执行。

CPU/WASI 策略保持 `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9`，镜像内可信 bundle 为 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`；没有复用宿主 receipt 或用缓存文件代替镜像内启动检查。

## 最终复核

`test-results/g41-audit.json` 的只读复核通过：最终 worker 的 304 个源码文件、原控制镜像的 128 个源码文件及 Compose 记录与当前源码匹配；36 个已记录的自有容器不存在，十个独立检查标签及 Compose 镜像标签已移除，三组原进程出生身份均已消失。四轮 worker 检查的临时目录、容器、网络和卷均已清理，首次失败也保留记录。原有 `praximesh-web-1`、`praximesh-worker-1`、`praximesh-tutor-1` 全部运行且健康，没有重建。

Ruff 检查和 666 个 Python 文件格式检查通过；迁移检查无新增变化。原控制器两项构建函数抽取前后的 AST 完全一致；集中部署指南五段 shell 命令通过语法检查。文档本地链接与 `git diff --check` 通过，运行日志、数据库、配置、截图和镜像制品均不进入提交。

## 剩余范围

专用入口和部署配置不表示当前站点已开放 GPU。真实新安装元数据、设备映射、完整 CUDA 套件、总时间预算、候选参考解/错解、性能及真实队列仍待硬件验收；升级、发布协调与本地 GPU 工具也继续交付。
