# Python、JavaScript / WASI 构建与本地判题

本方案面向 Linux x86_64、非 root 账号。需要 C/C++ 编译器、make、Git、pkg-config、patch、uv 及约 1 GB 构建空间。使用官方固定版本源码和工具链；不要求 Docker、runsc 或修改共享主机配置。决策和限制见 [ADR-004](adr/004-wasi.md)。

默认 `docker compose up --build -d` 已自动构建本文的固定制品并启动独立 worker，见 [Compose 指南](compose.md)。以下步骤供原生开发或正式发布时单独构建使用。

## 构建

```sh
uv sync --frozen --group judge --group browser
.venv/bin/python scripts/build_wasi.py --output var/wasi-python --workdir /tmp/praximesh-wasi-build --jobs 4
curl --fail --location --max-time 60 --output /tmp/praximesh-quickjs.tar.xz https://bellard.org/quickjs/quickjs-2026-06-04.tar.xz
.venv/bin/python scripts/build_functions_wasi.py \
  --python-bundle var/wasi-python \
  --python-sha256 "$(sha256sum var/wasi-python/manifest.json | cut -d ' ' -f 1)" \
  --sdk-archive /tmp/praximesh-wasi-build/wasi-sdk-24.0-x86_64-linux.tar.gz \
  --quickjs-archive /tmp/praximesh-quickjs.tar.xz \
  --workdir /tmp/praximesh-functions-build --output var/wasi-functions
```

两个目录都应是新的；脚本拒绝覆盖已部署制品。下载地址及 SHA-256 固定在脚本中。CPython 源码、随发行版要求的 WASI SDK 24、构建用 Wasmtime CLI 均校验后解包；标准库 ZIP 无压缩且条目排序、时间固定；包含源码以及由同版本可信 CPython 构建解释器生成的哈希字节码，减少 guest 的重复解析成本。整个 ZIP 受 manifest 摘要保护，不能加入用户提供的缓存；学员代码仍只在 WASI 内编译和执行。编译后的 raw Wasm 和本机 native cache 都纳入 manifest，保留 Python 许可。具体编译器/构建目录可能影响二进制摘要；这里保证固定来源和可重建步骤，不承诺跨主机逐字节相同。

当前使用 format 3 联合 bundle：CPython 3.14.9、QuickJS 2026-06-04 与 Wasmtime 50.0.1。第一步生成 format 2 Python 构建输入，第二步校验其文件及 QuickJS/SDK 固定摘要，在当前引擎策略下重新生成两个 cache。format 2 不能直接供 B2 worker 使用。显式 target 为 `x86_64-unknown-linux-gnu`，不推断构建机 CPU 特性；关闭未使用的 component model。`THIRD_PARTY_NOTICES.txt` 保存 wasi-libc、musl、cloudlibc、LLVM compiler-rt 的声明，`LICENSE.quickjs` 保存 QuickJS 的 MIT 许可。固定来源、WASI 兼容补丁见 `vendor/wasi/` 和 `vendor/quickjs/`。旧 bundle 与 receipt 不能手改 manifest 升级。

将脚本输出的 `JUDGE_WASI_BUNDLE`、`JUDGE_WASI_SHA256` 配置到执行节点。示例只展示读取**刚在本机可信构建产生**的 manifest；不能用同样方法信任陌生 native cache。

```sh
export JUDGE_BACKEND=wasi
export JUDGE_WASI_BUNDLE="$PWD/var/wasi-functions"
export JUDGE_WASI_SHA256="$(sha256sum "$JUDGE_WASI_BUNDLE/manifest.json" | cut -d ' ' -f 1)"
export JUDGE_RECEIPT="$PWD/var/wasi-receipt.json"
.venv/bin/python manage.py sandbox_check
RUN_SANDBOX_TESTS=1 .venv/bin/pytest -m sandbox -q
```

`sandbox_check` 必须真实通过，禁止生成假 receipt。记录有效期 24 小时，绑定主机、内核、运行时、制品与完整判题策略。依赖、引擎配置或制品更新后重新构建/测试，启动新的 worker。不要在运行中的 worker 所用虚拟环境里执行会增删依赖的 `uv sync`/`uv run`；使用固定 `.venv/bin/python`，或每次部署创建新环境。

## 重放产品闭环

```sh
.venv/bin/playwright install chromium
.venv/bin/python scripts/browser_acceptance.py --judge
```

脚本创建独立临时 SQLite 数据库、仅绑定回环地址的 Web 和真实 worker，先运行安全探针，再验证注册、草稿、公开 Run、错误提交、修正通过、路径与历史、账号删除。所有子进程和数据库在结束时清理，截图与受限日志位于忽略的 `test-results/`。普通 `scripts/browser_acceptance.py` 验证判题关闭时的流程。

持续本地体验可使用同一数据库分别启动 Web 和 worker，两端设置 `JUDGE_ENABLED=1`、相同 `JUDGE_BACKEND` 与 `JUDGE_WASI_SHA256`，然后运行 `.venv/bin/python manage.py judge_worker` 及仅回环监听的开发服务器。Web 不需要读取制品或安装 Wasmtime；它只向有匹配健康记录的 worker 排队。保存的历史记录包含实际 Python/运行时/制品摘要。

## 资源和生产运行

函数调用使用 128 MiB 线性内存、1 MiB Wasm 栈、最多一份 memory/table/instance、20,000 table 项、32 KiB 合计输出；整个请求最多 256,000 字节。Python 每次调用 50 亿 fuel，JavaScript 1 亿 fuel，QuickJS 另有 64 MiB 堆和 512 KiB JS 栈限制。fuel 包含解释器启动，耗尽也判时间超限。独立驱动地址空间上限 1.5 GiB、10 秒软/11 秒硬 CPU、15 秒墙钟、64 文件描述符；驱动本身也有资源成本，不能用 128 MiB 作为整台 worker 的容量预算。Python 完整程序先在独立 guest 中只做语法编译，再运行实际 stdin/stdout；两个阶段分别受上述限制。

JavaScript 只支持同步 JSON 函数，不提供浏览器或 Node API。严格拒绝 Promise、undefined、BigInt、访问器、循环结构、非有限数字和不安全整数。完整程序输出必须是单份有效 JSON，不接受额外日志、重复键或自报 verdict；正式答案始终由可信控制器比较。本地公开测试和服务端提交说明见[本地工具](local-practice.md)。

OS alarm 处理阻塞调用；父进程死亡信号处理 worker 异常退出。无可写 guest 文件系统，不需要磁盘写入或子进程配额来容纳学习者任务。临时输入是已打开的匿名文件，只作当前 stdin；不会授予 `/proc` 访问能力。所有期望答案留在外部控制器。

生产使用独立无其他业务凭证的执行账号/节点，最小数据库角色按[运维手册](operations.md)配置。示例服务为 `deploy/praximesh-wasi-worker.service.example`；其主机级 cgroup 配置须在目标环境实际验证，本地测试不代表 systemd/生产网络已验收。Web 继续执行 TLS、代理与注册限制。每日检查记录过期会停止 worker；在维护窗口停止 worker、跑探针并重启，不自动忽略过期门槛。

`wasi.yml` 定义在一次性托管 CI runner 构建并跑真实检查，随后执行 [HTTPS 部署验收](production-acceptance.md)；它不使用共享特权节点。gVisor 工作流仍只允许受保护的专用 runner。这些工作流尚未在托管服务端运行；临时回环 TLS/容量成功不能替代公有证书、目标生产节点、自动部署或运营许可。native cache 的跨节点兼容性尚未验证，目标节点需可信构建或实测兼容性后再启用。

worker 也可使用与 Web 同源的独立镜像，具体构建和导入见[发布制品指南](releases.md)。镜像入口在当前容器执行实际探针后才开始取任务，内部 WASI 能力和资源限制保持不变；无需 Docker socket，也不复用构建机 receipt。
