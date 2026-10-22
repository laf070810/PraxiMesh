# RFC-002：基础 Python 函数的 WASI 执行边界

状态：接受，决策见 [ADR-004](../adr/004-wasi.md)。日期：2026-10-22。关联：[RFC-001](001-phase-a.md)、[ADR-002](../adr/002-evaluation.md)、PRD R-06 / AC-04。

## 动机与范围

现有主机没有 runsc，rootless Docker 也没有 CPU quota。不能通过关闭检查或普通 Docker 回退来开放判题。另一方面，首批八道题只需要 JSON 参数、基础 Python 和 math；无须 Linux 系统调用或原生第三方扩展。为这一明确范围评估 CPython/WASI，不以本机便利性替代隔离要求。

候选为官方 CPython 3.14.9 源码构建的 wasm32-wasi 解释器，配合 Bytecode Alliance Wasmtime 50.0.1。继续保留显式配置的 gVisor 后端；任何后端失败都禁止自动切换。应用默认仍关闭执行。

## 必须验证的边界

- 只加载固定 digest 的可信解释器和标准库；不接受用户 Wasm、native artifact、checker 或命令。
- 用户源代码仅交给 Wasm 内的 CPython。WASI 只有只读标准库目录及有界标准输入/输出，没有宿主环境、工作区、数据库、网络 socket、进程或可写目录能力。
- 每用例新实例及独立宿主执行进程；Wasm fuel、线性内存、table、stack、宿主 CPU/内存/文件描述符、输出和墙钟上限；终止后回收。宿主子进程仅运行可信 Wasmtime 驱动，不能直接 exec/eval 用户代码。
- 标准库不得带外部符号链接；遍历、绝对路径、写入、环境、网络、进程、跨任务读取均需真实探针。
- 预期答案仍只在控制器内；返回值由既有版本化 evaluator 比较。伪造分数、退出码或输出不能绕过比较。
- 参考实现和有针对性的错误实现必须实际运行；端到端提交和进度必须由实际通过结果驱动。

WASI 会限制标准库中依赖网络、进程、线程或文件写入的功能，必须在题页说明实际 Python 版本与可用边界。不能宣称支持完整桌面 Python、NumPy 或额外语言。

## 维护与发布

构建脚本固定官方下载地址及 SHA-256，保存 Python/SDK/运行时版本和生成物摘要。运行时升级需重新构建、跑探针、参考/错误实现及浏览器流程；安全记录绑定后端、策略、解释器、标准库和运行时。默认禁用、短期有效的探针记录与 worker 健康检查继续适用。

Wasmtime 属于可信计算基；能力隔离不能消除运行时漏洞、侧信道或主机管理员风险。公开部署仍使用独立无业务凭证的执行账号/节点、最小数据库权限和操作系统进程限额，并及时跟进安全公告。真实测试通过也不替代公开发布、TLS、许可和运营决策。

## 官方依据

- [CPython WASI 构建与支持级别](https://github.com/python/cpython/blob/main/Platforms/WASI/README.md)。构建采用具体发行版随附工具，避免依赖开发分支布局。
- [Python 3.14.9 发布](https://www.python.org/downloads/release/python-3149/)。
- [Wasmtime 安全模型](https://docs.wasmtime.dev/security.html)、[Python API](https://bytecodealliance.github.io/wasmtime-py/)、[安全公告](https://github.com/bytecodealliance/wasmtime/security/advisories)。API 以固定安装版本的源码核验，不能照抄滚动文档的参数。

已完成固定源码构建、真实隔离探针、参考/错误实现及浏览器闭环验证，适用于这里定义的基础函数范围。资源、父进程故障和制品完整性检查见实现及[验收记录](../acceptance.md)。生产 TLS、容量、服务端 CI、独立安全复核和运营项仍需单独完成。
