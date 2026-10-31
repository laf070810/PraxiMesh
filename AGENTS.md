# PraxiMesh 开发指南

适用于本仓库。用户当前指令优先于本指南；更深目录存在指南时，也应核对其适用范围。

## 开始工作

- 先确认工作目录、Git 分支、暂存区和已有改动，区分用户工作与本次任务。不要覆盖、清理或重新暂存无关改动。
- 阅读 [README](README.md)、[PRD](PRD.md) 和[需求追踪](docs/requirements.md)，再查看相关模块、测试及 RFC/ADR。以当前源码和实际结果为准，历史验收只证明其明确记录的版本与范围。
- 保留完整产品目标。环境不足时说明具体缺口，并继续其他可独立完成的工作；不要用模拟结果、未执行的计划或改小验收范围宣称完成。
- 重大架构、内容、评测、安全、权限或部署变化先形成 RFC，决定后维护 ADR。索引在 [docs/README.md](docs/README.md)。

## 架构与实现边界

- 应用使用 Django、服务器模板和渐进交互；本地可用 SQLite，生产使用 PostgreSQL。沿用现有用户、稳定知识 ID、题目版本与进度，不为新题型复制独立产品。
- 内容校验与导入在 `core/content.py`；执行分派在 `judge/evaluator.py`；队列、租约与资源预约在 `core/queue.py`、`core/resources.py`；正式学习证据经 `core/result_evidence.py` 校验。
- Web 不执行学员代码，不接触 Docker socket。学员提交、参考解及错误变体只能通过已配置和验证的执行隔离运行，禁止宿主 `exec`、导入学员模块或普通容器回退。
- 评测端保留受保护的预期结果，不能信任 guest 或客户端自报的 verdict。区分用户错误、资源超限和平台故障；保持 Run、正式 Submit 和成绩证据的区别。
- 新评测语义、驱动或资源规则应核对 `judge/sandbox.py` 中的策略摘要范围，补齐相应真实探针。不能伪造 receipt、复用不匹配的策略记录或绕过健康、租约、取消与资源门槛。
- 源码、草稿、笔记、计划和非公开社区内容默认私有。新增用户数据时同步处理跨账号访问、CSRF/POST、限流、显式公开与撤回、导出和删除。AI 只作可选辅助，不能写正式成绩。
- 成绩分享由 `showcase/services.py` 每次验证并投影公开字段；新增指标必须逐条重新确认，不能扩大旧 `metric_kind` 的公开范围。GPU 只比较同版本、同完整环境的已公开正确 Submit，以显示到 0.001 ms 的分数处理并列，不公开原始采样或设备详情。核对迁移的旧写入兼容、实际数据库权限、中英确认表单和撤回；规则见[分享指南](docs/sharing.md)。

## 内容、迁移与发布

- 已发布题目、项目、课程和挑战使用不可变版本及固定摘要。修订追加版本；不改写旧数据来让历史成绩适配新规则。保持分类、依赖关系和学习路径相互独立。
- 新题提供独立题意、目标、约束、公开样例、受保护测试、参考实现、针对性错解、来源和授权信息。静态校验不替代真实正确/错误程序验证；遵循[内容规范](docs/content.md)。
- CUDA 内容校验复用 `judge/gpu_contract.py`，不能放宽 CPU schema 或加入 CPU 分派。`content/candidates/` 只存候选新增内容；`content_compose` 生成完整包但不发布、不覆盖 ID。参考解/错解的真实检查使用 [GPU 指南](docs/deployment/gpu.md#cuda-content)中的正常 API 验收入口，只针对独立测试站点和合成账号；观察超时保留请求键、任务和站点，不把协议替身或静态校验当作硬件通过。
- `content_import` 是发布动作，会撤回官方全量包中缺失的内容。只在新建或明确授权的数据库导入示例包。应用升级默认保留现有目录；不自动覆盖维护者或组织的内容。
- 数据库结构变化追加迁移，保留已交付迁移文件。新增表/应用时核对 `scripts/deploy_database.py` 的角色权限、`scripts/release.py` 的源码范围和 `Dockerfile` 的复制范围；Web/worker 不获得全表权限来规避授权错误。
- 发布包不可原地改写。新控制器必须能校验需要升级的旧制品；新增开发文档不得无意变成旧清单的必填项。schema 升级、失败恢复和内容发布分别说明验证范围。
- 界面字符串使用 Django 翻译，维护 `.po` 和编译后的 `.mo`；内容译文绑定准确的源版本。缺少译文明确回退，不能借用另一版本文本。
- 编辑器源码在 `frontend/`，按 [编辑器指南](docs/editor.md)更新 npm 锁文件、预构建静态资源和许可证；不能只改源码而遗留旧 bundle。保留 textarea 回退、私有草稿、异步修订保护及 CSP nonce，分析工具不得执行源码或外发代码。界面改动用 `scripts/browser_acceptance.py --interface` 验证中英、多种宽度、键盘操作与失败回退。
- CUDA 编辑器在构建时扩展锁定的 Lezer C++ 语法，保留上游字符串/注释解析与独立生成的 token ID；不得靠删除或改写用户文本消除误报。结构提示不能代替 NVRTC。修改后运行 `npm run test:editor` 及 `scripts/browser_acceptance.py --gpu-results`；后者只操作自有临时数据库，不执行 GPU。题库 CPU 调用单位与 GPU 整卡秒数分别筛选；公开 `.cu` 包不得走本地 WASI 或宿主执行回退。

## 环境与验证

在独立开发环境使用 `uv sync --frozen`，按任务增加 `--group judge`、`--group browser`。已启动的服务使用固定虚拟环境；不要对其运行 `uv sync` 或可能隐式同步依赖的命令。

常用检查如下，按实际改动选择：

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python manage.py content_check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pytest -q tests/test_content.py
```

- 先复现具体失败，再修改和验证对应路径。优先有意义的状态、权限、边界与恢复检查；无需为纯文字调整增加重复实现的测试。相关检查通过后，仅在新改动或未解决问题需要时扩大测试范围。
- 上例以内容检查为例，测试路径按改动模块选择。涉及 PostgreSQL 锁、角色权限或迁移时使用 `scripts/postgres_tests.py`，例如 `.venv/bin/python scripts/postgres_tests.py -q tests/test_community.py`；它创建并清理自有临时数据库。
- 真实评测先按 [WASI 指南](docs/deployment/wasi.md)配置可信 bundle、摘要及 `JUDGE_RECEIPT`，实际运行 `sandbox_check`。`RUN_SANDBOX_TESTS=1` 才启用相关集成场景；记录跳过原因，不把 skipped 当通过。
- GPU 基础环境检查使用 `scripts/gpu_environment_probe.py`，按 [GPU 指南](docs/deployment/gpu.md)显式选择维护者授权的空闲单卡。检查采用固定完整 runsc 归档、临时用户服务和独立存储；不能以 CLI 参数代替真实父 cgroup 限额和节流检查。可选 `--nvrtc-archive` 重放审阅过的内核参考解/错解，编译也必须在隔离内进行，受保护答案留在控制器。各模式串行执行，不生成正式评测 receipt；保留制品/源码身份、失败、最终结果和清理记录。
- GPU `--fault-probes` 只用于明确授权的独占卡，客户端超时后必须确认自有容器终止及设备无残留，并重跑正确解；不能终止未知进程或自动重置 GPU。`--image-archive` 可减少下载依赖，但必须校验固定清单与完整镜像层后导入独立存储，继续实际探针。CPU 内存配额、设备堆限制和整卡显存容量不能相互替代。
- GPU 调度在 `core/gpu_resources.py`，主机设备锁在 `judge/gpu_device_lock.py`。所有控制器副本须共用私有锁目录，不能删除锁文件；设备行先于任务行加锁，拿到锁后重新核对租约。超时、账号删除及部署恢复不能自动释放设备预约，只有持锁控制器完成真实清理与恢复后才能确认可用。调度测试不替代硬件检查，CPU 调用额度与 GPU 整卡时段预约分别记账。
- GPU 网页准入在 `core/gpu_admission.py`；`JUDGE_GPU_DEVICES` 默认空。Web 只读完整启动发布的环境、当前策略与新鲜设备状态，不读私有凭证或取得设备行写锁。设备换所有者时清空发布信息，旧迁移记录不能自动启用；排空必须包括所有持久设备预约，删除账号、结束任务或移除配置不能隐藏它们。改动需重放实际 PostgreSQL 并发、最小权限、发布事务和升级检查；合成环境不能用于实际开放提交。
- GPU 交互协议与控制器比较在 `judge/gpu_session.py`、`judge/gpu.py`；`--session-probes` 复用实际会话路径。管道夹具不证明 CUDA 或性能成功；GPU 性能只能使用控制器测量，不采纳 guest 的计时字段。文件锁不隔离其他账号的 CUDA 进程，发现外部占用时停止启动并等待独占条件，不能用低利用率代替空闲证明。
- GPU 服务监督在 `judge/gpu_service.py`，短事务回调在 `core/gpu_control.py`；仅可信控制器可传入启动命令，源码/答案/密钥不得放进命令行或日志。终止前核对预约及服务调用身份，终止后核对整个 cgroup；数据库等待失败不直接释放设备。服务控制改动用 [非 GPU 控制链检查](docs/deployment/gpu.md#不占-gpu-的控制链检查)和实际 PostgreSQL 验证；固定 CPU 进程树通过不替代 CUDA 清理、设备空闲与正确性恢复。
- 容器监督基础在 `judge/container_service.py` 和 `judge/container_entry.py`，尚未接入 GPU worker。仅可信程序可使用固定镜像，保留私有 rootless socket、空白客户端配置、最小环境、绝对期限和实际容器实例核对；不能把 runc 容器用于学员代码。按[集中检查入口](docs/deployment/gpu.md#container-supervision)重放固定 CPU 故障及清理；同名替换或无法确认停止时不得清空预约，不以此检查生成 GPU 凭证。
- 私有控制器命名空间与工作目录在 `judge/gpu_namespace.py`，socket 在 `judge/gpu_transport.py`；保持环境变量白名单、完全断网、内核对端/cgroup 核对及预约身份绑定。用集中指南中的命名空间验收入口重放实际连接和终止；未停止、身份不符或仍有挂载的目录不能递归清理。此控制通道只连接受信进程，不能代替 guest 隔离、设备恢复或正式准入。
- 共用离线制品与运行环境在 `judge/gpu_artifacts.py`、`judge/gpu_environment.py`，控制命令由 `judge/gpu_command.py` 有界执行。修改后按集中指南重放固定 CPU 的实际 runsc 验收；CUDA 仍须显式准备单卡和编译器。输出超限时先关闭并终止附着客户端，再独立核对、终止和等待自有容器；不能把关闭管道视为 GPU 清理完成。保留私有存储、空 hook 目录、固定镜像校验和子进程独立 home，不继承操作者配置来修复启动失败。
- 异常存储回收在 `judge/gpu_recovery.py`，固定入口为 `judge/gpu_recovery_entry.py`。先持锁确认原服务停止及 cgroup 为空，再用独立回收服务核对预约、目录 inode 和 UID/GID 映射；不能打开旧容器数据库、根据旧 PID 发信号或执行遗留目录中的程序。保留失败现场和身份记录，按集中指南重放实际 CPU 异常恢复；文件删除成功不证明 GPU 恢复，也不能直接释放设备预约。
- GPU 执行连接在 `judge/gpu_adapter.py`、`judge/gpu_controller.py` 和 `core/gpu_execution.py`。运行时身份核对及开始执行登记之前不发送源码；结果须等待独立正确解恢复、存储和整服务清理及设备空闲。错误清理保留同一服务实例的调用身份，不重建对象绕过核对。协议/PG 替身测试和准备前的实际拒绝检查不证明 CUDA、恢复或性能成功；正式启动 receipt 和产品准入仍须真实验收。
- GPU 启动恢复在 `core/gpu_startup.py`：接管先隔离停用，保留原预约，先停止全部已知旧服务再处理目录；无任务时仍按每卡固定身份找回中断的维护服务。所有副本与升级共用原私有工作根及锁目录。维护消息不含学员输入，恢复完成仍不能直接标记 ready 或签发凭证；修改后重放实际 PostgreSQL 与四项 CPU 异常恢复检查。`core/gpu_resources.py` 也在 CPU 策略摘要中，更新后须使用当前策略重新执行真实 WASI 探针，不能沿用旧 receipt。
- 启动资源检查在 `judge/gpu_isolation.py`，由维护协议调用，并由集中指南的 CPU/runsc 入口重放同一模块。核对完整九项检查、十七个容器清理及每次故障后的重新运行；CPU 模式不能满足 GPU 模式。PID 探针只执行 `judge/gpu_host_pids.py` 中的固定可信代码，在私有命名空间专用叶组实测宿主上限，不能代入学员输入、程序或路径，也不能把虚拟 guest 进程数等同于宿主任务数。部分结果、配置字段或单元替身不能生成就绪凭证。
- 固定 CUDA 启动用例在 `judge/gpu_kernel_checks.py`；维护请求只传已审阅用例名和随机挑战，不能增加任意源码或命令入口。核对预期 verdict、编译完成、实际容器身份和后续独立正确解恢复。编译后取消、附着客户端退出、准备故障和控制通道失联须分开；任意异常不能作为故障探针通过。CPU 管道测试不证明 CUDA 或容器清理，单项成功不签发就绪凭证。
- 整控制器故障协议在 `judge/gpu_lifecycle.py`；匹配本次就绪和触发挑战后才接受断开。硬期限必须在显式停止前观察整 cgroup 为空；失联则先观察仍存活，再监督终止。旧调用及目录清理成功前不得创建新维护调用。修改后重放集中指南中的[控制器故障检查](docs/deployment/gpu.md#不占-gpu-的控制器故障检查)，CPU 结果不代替 CUDA 恢复、完整总预算或凭证验收。
- 完整启动编排在 `core/gpu_readiness.py`，私有凭证在 `judge/gpu_receipt.py`。只有二十四个必需阶段和完整报告均通过，才能发布本次所有者的凭证并在行锁下确认原预约恢复；不从外部 JSON、部分报告或 CPU 结果生成运行凭证。保留双时钟期限、主机 boot ID、锁所有权和私有文件检查；空闲心跳核对实际设备并在同一事务内锁定身份和刷新健康。相关改动需真实 PostgreSQL 验证发布/就绪竞争；测试替身及临时文档不能作为 GPU 验收证据。
- GPU worker 生命周期在 `core/gpu_worker.py`，命令与配置统一见 [GPU 指南](docs/deployment/gpu.md#gpu-worker)。每次进程启动完整重检，领取和结算在设备身份行锁下核对当前凭证。信号处理器只设置标志；退出沿原服务实例清理、撤销就绪并保留尚未恢复的预约。取消后恢复前后均核对原预约，正常到期才可完整续检，损坏或身份变化不能当续期处理。修改后验证真实 PostgreSQL 竞争及自有子进程信号；不以运行时替身证明 CUDA 或容器化部署成功。
- GPU 持久成绩在 `core/gpu_evidence.py`，复用 `core/result_evidence.py` 的私有学习入口。按保存的版本化配置验证历史环境和摘要，不把旧执行摘要强制改为当前代码；既有 `PROFILES` 定义不可改写。GPU 正确性全部通过后重复批次仍可能失败，正式性能必须来自完整正确 Submit 的控制器采样。页面与导出只用已验证反馈，GPU 整卡秒数不能显示为 CPU 调用单位。真实浏览器检查入口见 [GPU 成绩展示](docs/deployment/gpu.md#gpu-results)；合成数据库和计时只证明展示与状态，不能签发运行凭证或声称 GPU 性能通过。
- 浏览器、HTTPS、发布包和升级用现有 `scripts/*acceptance.py` 入口。使用一次性数据库、合成账号和明确属于本次测试的容器/卷；默认使用回环地址，按用户明确要求验证直接 IP 访问时使用独立测试端口。完成后核对清理，不删除未知资源。
- Compose 单机入口为 `core/selfhost.py`，复用 `core/preview.py` 的原子数据库初始化；默认包括真实 WASI worker，可选 `compose.ai.yaml` 启动本地模型。修改时重放 `python3 scripts/compose_acceptance.py`；涉及模型或界面时使用 `--ai --browsers`。核对真实探针与七种题型、worker 离线关闭提交、直接 IP/CSRF、初始化恢复、旧内容不重导及成绩/会话跨重建保留。不得用旧 receipt 或宿主缓存代替镜像内自检。此入口不替代生产发布协调器。
- 重型 WASI/worker 验收按资源预算串行安排，避免争用造成假超时。长进程须跟踪原进程或工具句柄；观察超时不代表进程结束，不据此重复启动。
- 托管 CI、部署 runner 与 GPU 评测 worker 分别验证；接入步骤见 [CI 指南](docs/deployment/ci.md)。自定义 runner 标签不构成权限隔离，公开 PR 不能接触持有站点权限的 runner。GitHub 管理 API 查询失败不得当作 runner 不存在；部署 `status` 只验证记录读取，不证明站点健康。
- 保留原始失败和修复后的结果，记录代码/策略/制品身份。结果文件放忽略目录 `test-results/`，必要的结论写入对应验收文档；不以单元 mock 证明真实隔离、GPU、模型或部署成功。
- 文档变更核对链接、命令参数和当前行为。初始化、导入和恢复示例尽量在临时副本/数据库验证，避免改变开发者已有数据。

## 文档与 Git 提交

- README 面向首次使用者，说明当前能力、快速体验、真实评测、自部署与开发入口；历史执行细节放对应验收文档。保持需求追踪、RFC/ADR、使用指南和 CHANGELOG 与实际行为一致。
- 部署操作统一维护在 [docs/deployment/](docs/deployment/README.md)：首页负责方案和场景选择，专页负责对应操作。README 只保留最短启动入口，旧文档路径只做跳转；新增部署选项时同步更新选择表，避免在多处复制完整步骤。
- **工作过程中及时 git commit**：每完成一个可审阅的相关增量，就记录具体问题、变化和验证结果。提交前检查差异，只包含本次任务文件；未完成或未验证部分必须如实说明。用户未要求时不推送、不改写既有共享历史。
- 不使用 `git add .`、reset、clean 或 stash 来掩盖混合工作区。已经存在的用户暂存内容应保持原状。
- 本项目开发工作区可能仍保留未出生的 `master` 和用户暂存的 `PRD.md`，而实现历史保存在 `codex/full-product`。遇到这种状态，先用 `git log codex/full-product` 核对，不能把所有 untracked 文件当成没有历史。沿该审阅分支追加提交，使用临时索引或隔离工作树，保持原 HEAD 和索引；不要为方便提交擅自切换或重置用户工作区。正常检出的分支按其实际状态提交。
- 不提交 `.env`、密钥、恢复码、数据库、真实用户代码、运行凭证、缓存或测试截图。临时日志可能包含敏感内容，保存和分享前检查。

## 尚未具备的外部条件

GPU 题型、托管 CI/部署目标与公开运营/授权事项以[需求追踪](docs/requirements.md)为准。没有经过验证的 GPU 隔离、资源预算和真实正确性/性能检查时，不开放 GPU 提交。不要自行修改共享 daemon、驱动、主机服务或批准项目许可证；在已有授权内完成可逆开发，新增外部运营决定由维护者明确。
