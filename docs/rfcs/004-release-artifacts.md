# RFC-004：一致版本的应用与判题发布制品

状态：制品方案接受并实测通过；部署协调待实施。日期：2026-10-23。关联：PRD §8.2、R-09、AC-07。决策见 [ADR-006](../adr/006-release-artifacts.md)。

已有真实 HTTPS 验收仍从工作区运行 Web 与 worker；它没有证明发布后的二进制、源码、依赖、题目包和执行器属于同一版本。当前 native Wasmtime cache 还默认使用构建机器的 CPU 特性，不适合作为未验证的跨节点制品。

## 选择与边界

采用固定基础镜像、同一源码快照产生两个非 root 镜像：Web 不包含 Wasmtime/解释器制品，worker 包含固定依赖与 WASI bundle。普通容器只是控制器的部署边界；学习者代码仍逐用例在 WASI 中执行，不能回退到宿主 Python 或普通容器内 Python。worker 不挂 Docker socket，继续执行全部能力、资源和故障回收检查。

显式指定 Wasmtime 的 Linux x86_64 target，关闭构建机 CPU 特性的自动推断；引擎策略改变必须重新可信构建解释器 cache、重新运行真实探针和参考/错误解。此选择不声称覆盖所有内核、CPU 或硬件；目标节点仍需执行自己的安全门槛。

打包 worker 的入口为 `wasi_service`：要求显式启用 WASI，先在当前容器运行全部实际探针，再启动已有 worker；不把构建机 receipt 放进镜像，也不接受伪造的预通过标记。失败时进程退出，不发布健康状态。Web 与 worker 分别使用自己的运行凭证，凭证不进构建参数或镜像。

发布工具从明确的源码目录白名单建立独立快照，拒绝符号链接、密钥配置和运行状态；固定文件摘要后再构建。发布目录包含审查用源码档案、两个镜像的 Docker archive 和 manifest，记录源码身份、应用/worker image ID、WASI manifest、策略与锁文件摘要。验证必须提供从可信渠道取得的 manifest SHA-256；不能把从同一个陌生下载中计算的摘要当作真实性证明。任何文件、标签、镜像或版本不一致都应拒绝。

镜像只包含程序和默认内容包，不包含数据库、账号、receipt 或运行凭证。内容包的发布与数据库 schema 迁移须明确执行，不能因为更新镜像就自动覆盖运营内容。历史题目修订和提交仍留在数据库。第三方运行时声明随制品保留；本项目代码/内容许可尚待维护者确认，不因此自动发布 registry 或公开下载。

## 交付顺序与替代方案

先实现可构建、验证、导入和实际运行的不可变制品，并在一次性容器中重放真实 worker/应用功能，覆盖篡改拒绝与清理。这是后续自动部署及回滚所依赖的身份边界。

随后完成部署协调：停止新评测入队、排空/处理在途任务、独立备份与迁移、候选版本检查、激活、失败恢复。不能让两个不兼容判题策略无约束地争抢同一队列；不能把应用回滚等同于数据库逆向迁移。具体协调策略在形成实现前补充 RFC，不将当前制品工作冒称已经完成 CD。

没有采用共享目录原地 `git pull`/`uv sync`，因为它会让运行中的 Web、worker 和磁盘策略摘要失配；没有引入 Kubernetes，因为单实例阶段尚无调度需求。先使用可导出的镜像 archive，避免预设 registry 或云供应商；同一镜像身份可随后进入选定 registry 和受保护 CI/CD 环境。

## 验证

实际构建两个镜像；检查用户、只读根、无 socket/秘密；在容器内用真实 WASI 探针、正确/错误解验证执行与隔离；通过匹配的 Web/worker 处理实际任务；拒绝修改后的源码、manifest、镜像档案和不匹配的标签/制品；输出不含凭证的证据。更新运行时策略后重新跑全部相关测试。托管 CI、目标机器发布及公有站点仍独立验收。

## 官方依据

- [Wasmtime target 配置](https://docs.wasmtime.dev/c-api/config_8h.html)：显式 target 关闭宿主 CPU 特性自动推断；本地固定版本 Python API 文档已核对同一语义。
- [Docker 多阶段构建](https://docs.docker.com/get-started/docker-concepts/building-images/multi-stage-builds/)与 [Dockerfile](https://docs.docker.com/reference/dockerfile/)：默认目标仍为 Web，worker 为单独目标。
- 本次核验 [GHSA-32h6-97mm-8q3c](https://github.com/bytecodealliance/wasmtime/security/advisories/GHSA-32h6-97mm-8q3c)：公告列出的受影响版本不包含本项目固定的 50.0.1。本项目不接收用户 Wasm/component，且现在显式关闭 component model；此核对不代替后续公告跟踪。
