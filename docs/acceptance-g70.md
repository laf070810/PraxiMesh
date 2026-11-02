# G70：父镜像清理、桥接退出同步与无设备工具验证

日期：2026-11-02。对应 PRD R-09/15；起始提交 `36cf863`，清理修复提交 `703b9f0`，退出同步修复提交 `c47eb8f`。继续不查询、映射或执行 GPU，不改动现有站点服务，不导入旧 F 镜像，不推送代码。

## 已复现的问题与修复

复查当前 worker 验收的清理代码，发现 15 个构建或验收入口仍使用 Docker 默认 `image rm`；它可能在删除子镜像时递归删除无标签父镜像。[G44](acceptance-g44.md) 已记录过该行为造成旧 F 缓存丢失，但此前修复只覆盖派生 Web 夹具。

本次使用有明确标签的 G69 控制镜像作为只读基镜像，创建两个从未启动的自有容器，再生成自有无标签父镜像和子镜像。`test-results/g70-image-cleanup-before.json` / `.log` 记录默认删除子镜像后，自有父镜像随之消失；原基镜像保持不变。未执行这些镜像中的程序，也没有 GPU 映射。

全部 15 处调用增加 `--no-prune`，包括 `scripts/release_gpu.py` 的失败清理。相同结构的 `g70-image-cleanup-after.json` / `.log` 确认子镜像已删、父镜像保留；随后仅按本次记录显式清理测试父镜像。两轮合计四个自有容器和四个派生镜像均已清理，原基镜像及标签保持不变。没有修改全局 prune 或 daemon 配置。

`g70-components.xml` / `.log`：**367 项通过，0 跳过，0 失败**，覆盖发布、迁移夹具、Web 派生检查、容器监督/运行/编译/驱动、控制器、worker 与私有配置。全仓 Ruff 检查和 798 个 Python 文件的格式检查通过。没有用模拟设备结果声明 GPU 验收完成。

## 实际 WASI 本地工具

新建私有 receipt，使用完整清单摘要为 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d` 的已核验 bundle。`test-results/g70-wasi/probes.log` 的 **129 项真实隔离探针全部通过**，随后 `cli.xml` / `cli.log` 的 **36 项 CLI 检查全部通过，无跳过**。其中真实本地公开测试同时验证正确与错误程序，以及非正式结果边界；公开包下载传输在该用例中为明确测试替身，不据此宣称新的 HTTP/API 全链路验收。

`g70-wasi/result.json` 记录实际命令退出和新 receipt 身份；没有复用旧凭证。临时数据库路径已清理，未连接既有站点数据库。此结果补齐 G69 中需要显式 WASI bundle 而跳过的一项检查，不把两轮用例数累加成独立覆盖数。

## 真实编译暴露的退出竞态

仅镜像清理修复后的 `g70-compiler-build/` 成功构建并验证正确源码与语法错误。首次完整公开 CLI 检查 `g70-compiler-cli/` 却在第二项语义错解编译时返回平台不可用。两次限定诊断分别得到五次底层调用、十次独立 CLI 编译成功，未解释首次失败；不计为修复。

保留实际公开入口、加入仅捕获异常的观察器后，`g70-compiler-cli-observed/` 在第 22 项 SIGKILL 恢复后的新编译再次失败。异常栈指向结果已经确认后的 `ContainerConnection.finish()`；原容器为 `exit_code=0`，没有 OOM。随后 `g70-compiler-bridge-diagnostic/` 在第 20 次调用再次复现并捕获原桥接进程 **退出码 137**。之前的成功调用没有改变源码、检查或预算。原始失败和状态都保留在对应私有输出目录；没有查询、映射或执行 GPU。

原因是可信主控制器在关闭 socket 后立即返回，PID 1 随之结束，尚未正常退出的桥接进程可能被终止。`g70-bridge-exit-before.xml` / `.log` 的三个真实子进程/管道检查在旧实现上均失败，其中一个直接证明 socket EOF 后控制器仍过早返回。

按 [RFC-021](rfcs/021-gpu-container-supervision.md) 和 [ADR-039](adr/039-container-control-transport.md)，连接接受时打开并复核原桥接进程的 pidfd；关闭时在原期限内最多等待三秒，所有分支关闭句柄。宿主仍要求完整协议、EOF 和原进程零退出，不接受 137 作为成功。`g70-bridge-components-fixed.xml` / `.log` 的 **88 项通过，无跳过**，包括七项新增的实际进程生命周期和拒绝边界。首次命令误写了不存在的测试路径，未运行测试，日志 `g70-bridge-components.log` 保留。

修复后 `g70-current-components.xml` / `.log` 的 **634 项通过，无跳过**，覆盖传输、编译、调用、内核检查、生命周期、发布、worker 与配置；该范围包含前面的组件检查，不累计为互不重复的用例数。

`g70-compiler-fixed/build-result.json` 记录重新构建的匹配镜像 `sha256:1caac9b3cb30e70464635c837e87f9816b45e57ac9aaa8308886ef142fb796de`，编译契约 `0baaaf07a23d569902b790d72fbc1fc079ac70e9f7f99d2aed591960fe2b1395`；正确源码与语法错误均通过。随后直接使用未插入观察器的公开 CLI，`g70-compiler-cli-fixed/result.json` 的 **22 项全部通过，14 个原任务已清理**，包括两份参考解、六份语义错解、语法/架构错误、镜像/并发/包拒绝、SIGINT/SIGKILL 及其后恢复与新编译。NVRTC 仅编译，未执行 PTX 或产生正式成绩；语义错解能够编译不表示答案正确。

共同通信入口的 `g70-transport-fixed.log` 与 `px-container-transport-0e4360e4074e/result.json` 记录 **九次真实 CPU/gVisor 调用全部通过**：1 MiB 双向消息、身份错配、超长帧、运行中断连、客户端退出、取消、额外消息、独立期限和新调用恢复。九个自有容器、原进程树及自有镜像标签已清理；没有放宽权限、期限或资源预算。

## 当前 worker 镜像预检

清理修复前，`g70-worker.log` 与 `px-container-worker-d9797b71893e/result.json` 已通过十项真实 Docker/Compose/PostgreSQL 检查。清理与退出同步改变源码契约后，重新构建并完整重放，`g70-worker-fixed.log` 与 `px-container-worker-525c22267567/result.json` 的 **十项再次全部通过**。匹配镜像为 `sha256:512973fbb631a2b8b2592d46c1f08b58764aa006de8d25a6482491b8fd7f7fc4`，worker 契约为 `ee866164e9af292a8d49d6806b1228c99fa5e20fb9419eac33a65bdfc2126d3d`。

检查覆盖最小数据库权限、重建、显式与默认禁用、公开配置拒绝、本地状态拒绝、列权限越界拒绝、源码篡改拒绝、恢复原源码及旧 v1 控制配置拒绝。两轮各自的 11 个容器、数据库卷、网络和临时镜像标签均由入口清理，不把重复场景累加为独立覆盖。此入口没有映射或查询设备，也不启动完整 GPU 准入，不能替代真实 CUDA worker 和整站部署验收。

## 本地双镜像与资源清理

公开构建入口以当前源码重新生成 `g70-local-images/local-gpu.json`，权限为 0600。实际镜像配置和私有清单验证通过，原始输出在 `g70-local-images.log`：

| 用途 | 不可变镜像 ID |
| --- | --- |
| 本地控制镜像 | `sha256:1f4a38a95b09ceec57cbcd2f49ad4fd043784d971cea9f289b1de0c8c84daec2` |
| 本地任务镜像 | `sha256:020d257a33599e2d48f1a19a0a67c43ff70212da270ffb330389c15b2f1f7486` |

控制契约为 `9cf3b96f5c5ad7a7da5526f5a70da552b1c8b0d1df324f2ee944642a6161ffb5`，任务契约为 `a8ff880e4f590b7e1f71c0c73422735b5ba8c015443ae4f6ef074546b4a7ddc5`。本次只读取并复制固定归档及已安装库，不加载驱动、查询或映射 GPU；构建结果保持 `ready=false`。它不证明完整设备初始化、公开样例执行或正式评测通过。

`g70-resource-audit.json` / `.log` 按本轮原记录核对 **104 个自有容器全部消失**；69 个原编译任务均为已移除终态，无活动记录。另行核对已记录的原进程树、worker 网络/卷及临时镜像标签；清理夹具的四个派生镜像消失，原固定基镜像保留。新编译器及上述双镜像按构建目的保留。原 Web、worker、tutor 三个服务的容器 ID、镜像和启动时间与本轮开始完全一致。

## 编辑器与页面复核

`g70-layout-audit.json` 只读比较 `9da2bdd` 至当前的页面、样式、编辑器、译文和编辑器浏览器检查，源码未变；[G61](acceptance-g61.md) 的中英 25 页 × 四种宽度（320/390/768/1440）共 **200 组布局**仍对应这些界面。核对原报告并实际查看英文 320px 目标选项和中文桌面社区截图，标题、正文与选项对齐，未见新增横向溢出。已有高亮、补全、检查、格式化、快捷键及私有草稿验证见[编辑器验收](acceptance-editor.md)。这不是一次新的浏览器运行，也没有重建既有站点。

## 源码身份与完整范围

仅清理修复后的 `g70-contracts.json` 确认 CPU 与 GPU 执行政策、基础 runtime 契约不变。因 `container_runtime_acceptance.py` 同时提供受冻结约束的构建函数，其文件字节变化会改变控制器、worker、编译器和任务契约。后续桥接退出修复还会改变 GPU 执行和 runtime 契约；`g70-identity-docs.json` 确认最终 GPU 执行摘要为 `a1e876cbef0bfa65b56fd82b567fc1e57275a3b85a0cbf1c7e9561bac8d3fba2`，CPU 策略仍未变。旧制品仍按其原记录核验，当前调用须使用重新构建的匹配镜像。

全仓 Ruff 检查和 800 个 Python 文件的格式检查通过。`g70-source-package.json` 记录实际冻结的 970 文件源码快照、归档与逐字节读取检查；新检查、修复源码及文档均包含在内，临时目录已清理。文档链接、源码范围与有序策略身份另见 `g70-identity-docs.json`；没有修改既有发布归档。

GPU 完整启动、真实公开样例、性能、设备故障恢复、队列和整站部署升级仍待授权后的硬件验收；网站 GPU 准入未开放。旧 F 应用升级、托管 CI 和维护者外部事项仍按[需求追踪](requirements.md)保留，完整 PRD 未完成。
