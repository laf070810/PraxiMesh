# Python 完整程序 JSON 输入输出契约

`python-stdio-v1` 执行完整 Python 程序。题目使用 `language: "python"`、`output_format: "json"`；每个用例提供 UTF-8 字符串 `stdin` 和 JSON 值 `expected`，不使用函数名或 `args`。`comparison` 继续选择 exact 或 numeric 及其容差。题目应明确合法输入域，不能把未说明的格式或舍入要求藏进测试。

程序的真实标准输入描述符只包含当前用例的 stdin。`input()`、`sys.stdin.read()`、`sys.stdin.buffer.read()`、`os.read(0, ...)` 均可读取它。程序可使用受限 CPython 标准库；不提供网络、宿主文件、文件写入、进程、线程、原生扩展、任意 shell 或学员代码的宿主编译。

完整 stdout 必须恰好是一个 JSON 值，允许首尾 JSON 空白。UTF-8 解码失败、无输出、多份 JSON、额外解释文字、重复对象键、NaN/Infinity、指数溢出到非有限值、深度超过 32 或值节点超过 20000 都是 runtime_error。仅有 `null` 是有效 JSON 值；是否正确由题目 expected 决定。stderr 不参与答案比较，但计入输出字节预算。隐藏用例的输入、输出、异常正文和名称不会进入公开反馈。

Python/JavaScript 函数模式也使用同一严格 JSON decoder，并要求唯一的 `value` envelope；模型对象中的重复键不能在解码时静默覆盖。函数 envelope 不占返回值的深度预算。固定 CPython 的标准库及 lib-dynload 搜索路径都映射到同一份只读 bundle 内容，启动时不会额外打印缺失路径警告；输出预算完整保留给学员 stdout 和 stderr。

每个用例分两步，在独立 WASI 实例内运行：

1. 编译检查只调用 guest CPython 的 `compile`，不执行学员源码。此阶段的 SyntaxError 可以可信归类为 syntax_error，MemoryError 可归类为 memory_limit；启动、artifact、原生 runtime、驱动协议故障仍是平台故障。
2. 程序阶段在 guest 内重新编译并执行，stdout 作为原始答案数据交给可信 controller 严格解析和比较。`sys.exit()`、`sys.exit(None)`、`sys.exit(0)` 是正常结束，但必须有合法答案；`sys.exit(False)` 与 Python 的零退出语义一致。非整数退出对象（包括 0.0）和任何非零退出均为 runtime_error。

程序阶段**不信任学员退出码**。`os._exit(3/4/70/71/72/73/74/75)` 等均为 runtime_error，不能冒充语法错误、资源终止或平台故障。已经输出正确 JSON 后非零退出也不能通过。`os._exit(0)` 只有在实际写出的完整 stdout 是合法 JSON 且答案正确时才通过；它不会自动 flush Python 缓冲区，这与普通进程语义一致。程序阶段未捕获的 MemoryError 也是 runtime_error，不能从不可信退出码声称已区分 OOM。只有可信编译检查或 host 可证实的资源终止才使用对应资源分类。

两步都不在宿主执行或编译学员源码。controller 从不把 expected 或数据库凭据传入 guest；实际比较与 verdict 只在服务器允许列表中的 evaluator 完成。gVisor 后端显式拒绝本模式，不回退执行。

每一步沿用 WASI 限制：128 MiB 线性内存、5000000000 fuel、15 秒等待、host CPU 10 秒、host 地址空间 1536 MiB、fd 64。两步的时间相加记录为用例 elapsed_ms，因此最坏等待预算为两步各 15 秒；中间启动/清理另有有界等待。整份请求（source、stdin 与 JSON envelope）至多 256000 字节。程序 stdout+stderr 总共至多 32768 字节；燃料、wall、CPU 和输出限制由可信 driver/controller 判定。父进程死亡会终止驱动，实例间没有学员持久状态。

worker 的联合隔离 receipt 同时覆盖 Python 函数、JavaScript 函数及 Python 完整程序；任一模式探针失败不得启动 ready worker。加入本模式改变代码策略摘要和 receipt suite，旧函数 receipt 不能授权新代码。固定 CPython/QuickJS bundle 与 engine policy 未变化时可以复用 artifact，仍须在目标 worker 上重新生成 receipt。
