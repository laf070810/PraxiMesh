# G68：本地宿主状态与原控制容器生命周期

日期：2026-11-02。基线 `01a8dee`，原状态增量提交 `db976a0`。本次继续不访问 GPU，不查询 NVIDIA/NVML、不映射设备、不初始化 CUDA；既有站点未重启或重建。

## 实现范围

宿主命令在原 journal 中持有独立客户端锁，绑定原 v4 目录、物理锁、安装、socket、daemon 与本地标记。客户端锁不替代物理锁：创建和启动意图在原物理锁内保存，启动前释放物理锁，由容器内入口取得；恢复外层控制器时不先争用它持有的物理锁。

外层调用先保存固定创建参数和镜像配置，再登记完整 ID、创建时间及配置摘要，启动意图持久化后才允许附着。首次观察保存原启动时间；后续同时按名称和完整 ID 核对，拒绝改名、替换、重启和配置变化。停止、移除分别先落意图；创建或移除响应丢失时只确认原对象。重开旧意图只能清理，不启动旧镜像。已移除外层记录保留归档，后续显式恢复仍须通过新固定入口处理原内部操作。

源码见 `judge/container_practice_host.py`、`judge/container_practice_controller.py`；设计见 [ADR-078](adr/078-local-controller-host-ownership.md)和 [RFC-024](rfcs/024-local-cuda-practice.md)。操作只在[集中 GPU 指南](deployment/gpu.md#local-controller)维护。

**这仍是内部组件交付。** 完整管道调用、退出码与原内部终态联合确认、公开 CLI 的构建/初始化/执行/恢复，以及真实 GPU 验收仍待完成。控制容器移除不等于内部 GPU 任务已恢复；本次不签发 ready、成绩或性能证据，现有 `compile` 和 WASI `test` 用途不变。

## 原始失败及修复

- `g68-controller-before.xml/.log`：首次 26 项模型检查失败，Docker inspect 的数组误用了只接受对象的帧解析器；改用有界 Docker 响应的 JSON 数组解析后通过。
- `g68-local-controller-1/` 及 `g68-local-controller-diagnostic/`：真实创建后、启动前拒绝。诊断 inspect 显示固定交互创建的 `StdinOnce` 是 `true`，模型此前错误假定 `false`。修正后严格核对一次性 stdin 及三个附着流；没有放宽为任意布尔值。
- `g68-mount-order-before.xml/.log`：单项复现仅改变挂载清单展示顺序即错误拒绝。摘要改按目的地排序，仍保存全部挂载字段并核对唯一目的地、来源、读写与传播配置。
- `g68-local-controller-2/`：进入实际运行后，rootless Docker 将 `OomScoreAdj` 从默认 0 变成 200，并将未关闭 OOM 的 `false` 表示变为空值。现在显式指定 200，只归一化后两种未关闭 OOM 的表示；真正关闭 OOM、整数冒充布尔值或改变预算均拒绝。创建、运行及清理原始身份保留在输出目录。

以上失败均已结束并清理自有容器、状态及标签，未启动任何 GPU 任务。失败文件未被后续成功覆盖。

## 组件及实际容器检查

最终 `g68-components-final.xml/.log` 共 **336 passed、0 skipped**：包含原状态、固定协议/入口、控制器、worker 配置及发布规则。新增宿主状态 24 项使用真实文件/锁/子进程，新增原容器生命周期 38 项使用真实状态及明确的 Docker 模型；模型不作为实际容器或 GPU 成功证据。中间专项文件单独保留，不重复计入总数。

`g68-local-controller-final/result.json` 的五组实际 CPU 容器检查通过：创建后移除、附着后停止、改名拒绝、实际移除后的响应丢失，以及宿主客户端 SIGKILL 后恢复仍运行的原容器。镜像内检查实际 UID/GID 映射、只读根、能力、cgroup 配额、只读 procfs 与同一物理文件锁。此入口明确替换为固定 CPU 测试程序并去掉所有设备参数，使用合成设备声明；它不执行生产 GPU 入口的正向协议，也不读取 NVIDIA。

前一成功轮 `g68-local-controller-3/` 的 SIGKILL 留下一个空客户端配置目录，经所有者、权限、原容器创建/启动时间窗口及无活客户端核对后移除，记录在 `g68-client-config-audit.json`。验收入口随后将客户端配置限定在本次新建输出目录，最终完整重放确认 `client_configs_removed=true`。`g68-resources.json` 核对全部失败/成功轮的 14 个容器已移除，无固定夹具进程或容器残留；临时状态和测试标签已清理，原三个站点服务的 ID、镜像、启动时间未变。

最终固定 CPU 夹具镜像为 `sha256:dee379badb586c41ea95dd624462c88c467e4c046d3c6be8e39afda39120f766`，控制器源码契约为 `cf4ccea91975e7048b4e2572d7ab8f3e30d1485f361aa66a0076ad3b21e639ce`，GPU 执行源码摘要为 `f88730044c8a9a1e264712148f0f4e9d788a2a9f50bc0dfbc48a133e32a23f64`。实际检查后再次核对与最终工作树一致；这是测试镜像身份，不是可启用 GPU 的发布镜像。

## 源码与兼容边界

新增宿主模块进入有序 GPU、控制器与 worker 源码范围；CPU 执行策略未变。源码快照实际归档后重新校验新增模块、验收入口与固定 CPU 夹具的字节，临时归档副本已删除。证据为 `g68-identity-docs.json` 和 `g68-source-package.json`。

`g68-frozen-cpu.json` 由当前核验器只读验证原 `var/releases/2026-11-02-g59-cpu-2`，清单摘要 `931ab92cf73512a5372e14fa4fc976934e57d79b8250369dd4aac81a4ddf1da1`；没有导入镜像或执行归档代码。本次不重复声称完整 Compose、WASI、浏览器、PostgreSQL 或 GPU 产品验收通过；这些分别保留其已有版本范围。全目标中的公开 CLI、硬件、托管 CI 和外部运营门槛继续保留。
