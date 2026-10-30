# ADR-006：同源的 Web 与 WASI worker 不可变制品

状态：接受，制品导入与真实容器闭环已验证；后续激活/恢复协调见已实现的 [ADR-008](008-deployment-generations.md)。日期：2026-10-23。依据：[RFC-004](../rfcs/004-release-artifacts.md)。

从明确文件白名单捕获一次源码快照，再构建同源 Web/worker 镜像。发布 manifest 固定源码文件摘要、两个 image ID、WASI/策略身份、档案大小与摘要；源码档案拒绝越界路径、链接、重复条目和超限解压。导入前检查每个镜像配置和全部 layer 与 diff ID 的一致性，不依赖构建机器已有的 layer cache。调用方必须另有可信 manifest 摘要，当前没有发布者签名或托管 attestation 的证明。

Dockerfile 默认目标为 Web，不含 Wasmtime 或解释器。独立 worker 目标安装固定 judge 依赖和 bundle，不需要 Docker 客户端或 socket。测试运行配置为非 root、只读根、删除 capabilities、init 回收进程、内存/PID 限额；worker 控制器只连接内部数据库网络。Docker 封装可信控制器，用户代码继续仅由 WASI 执行，能力、fuel、CPU/内存/墙钟与文件描述符限制保持有效。

worker 入口 `wasi_service` 在当前实例运行真实探针，通过后才启动原有 worker；错误 backend、未启用状态、错误制品或探针失败均停止启动。镜像不包含构建机 receipt 或运行凭证。Web 与 worker 的实际策略摘要在运行实例内再次核对，避免仅凭标签宣称一致。

WASI bundle 升为 format 2，显式目标 `x86_64-unknown-linux-gnu`，禁用 component model；不再自动按构建机 CPU 特性生成 cache。解释器、SDK 与 Wasmtime 版本不变，重新构建并执行了全部真实测试。增加固定运行库的许可声明与来源摘要；旧 bundle/receipt 不能跨策略复用。其他 CPU/内核和目标主机仍须实测，架构名不能替代兼容性证明。

选择镜像 archive 作为首个传输格式，不预设 registry；镜像导入、内容发布、schema 迁移和服务激活分开。独立导入验收覆盖全新合成 PostgreSQL、受限角色、实际浏览器 Run/WA/Accepted/进度/删除及错误摘要拒绝，不能单独证明生产迁移、在途任务排空或恢复。后续协调器按 ADR-008 处理这些状态，不原地覆盖运行代码，也不让不兼容 worker 同时抢任务。具体结果见[验收记录](../acceptance.md)，操作见[制品指南](../deployment/releases.md)。
