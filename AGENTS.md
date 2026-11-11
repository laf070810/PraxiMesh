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
- 发布包不可原地改写。新控制器必须能校验需要升级的旧制品；新增开发文档不得无意变成旧清单的必填项。源码快照须独立于调用者 umask，让非 root 镜像可读管理命令、迁移和静态资源；只归一化白名单源码的内部目录/文件权限，保留外层工作区和配置私有。schema 升级、失败恢复和内容发布分别说明验证范围。
- 协调器恢复时的无状态 Web 重建在 `scripts/deploy_web.py`；保留实际维护路由、原数据库及严格运行契约。先保存原 ID/配置摘要与意图，移除只用已核对的完整 ID，新容器保存 ID 后才启动；不能按同名认领替代，也不能将此流程套用于 worker。按[集中恢复检查](docs/deployment/production.md#web-recovery-check)验证真实端口和中断，再重放完整 CPU 发布矩阵。固定 HTTP 专项可使用已有自有 Compose Web 的不可变镜像 ID，仅从镜像派生测试命令；不改动运行中的容器，清理保留基镜像，结果不替代冻结发布包的完整应用验收。
- GPU 发布使用同一冻结源码的四镜像 format 2，默认 CPU format 1 保持兼容。构建与核验在 `scripts/release.py`、`scripts/release_gpu.py`；有序源码契约从归档字节计算，导入前核对完整镜像层、角色和入口，不能执行归档代码或借用当前工作树/daemon 缓存。按[发布指南](docs/deployment/releases.md#gpu-release)串行验证导入后的无设备 worker 和 CPU 应用。制品通过不代表 GPU 激活或升级通过；未接入完整 GPU 生命周期的协调器必须拒绝激活。
- GPU 协调器节点绑定在 `scripts/deploy_gpu.py`，操作集中在[原节点绑定](docs/deployment/container-gpu-worker.md#coordinator-binding)。先保存意图再认领同一私有状态和锁，只允许 `attach-gpu` 为原配置增加 GPU 字段；不得借此改密钥或接管已有 worker/任务。绑定未确认前不得触及数据库或执行发布变更。`--prepare-only --initialize` 仍读取真实 GPU，需要授权；`deploy_gpu_acceptance.py` 才是合成元数据与实际文件锁/SIGKILL 的无设备检查，不生成就绪证据。
- 协调器 worker 的代际私有记录在 `judge/container_deployment_config.py` 校验，绑定原 owner、配置、数据库角色、发布及镜像契约；保留未绑定独立配置的兼容。Django 初始化、命令开始与取原锁之后比较完整配置/代际快照；不得遗漏仅元数据变化。新协议须纳入 GPU 与 worker 源码范围，旧包验证不等于支持新协议。内部持久发布在 `scripts/deploy_gpu_config.py`：原协调器/设备锁、实际维护路由、停止且不重启的原 worker、无活动 journal，以及原库/目标库的同 token 暂停、无预约检查及设备表/队列行锁必须同时成立。发布中标记阻止所有配置读取；恢复只继续同一目标，完整标记也要重新 fsync。不能清理未知临时文件、手工补记录或删除标记；旧制品核验不授予新协议能力。按[代际配置说明](docs/deployment/container-gpu-worker.md#generation-configuration)分别验证实际文件/SIGKILL、PostgreSQL 和完整生命周期，未通过后两者不能开放 GPU 激活。
- 协调器内部 worker 生命周期在 `scripts/deploy_gpu_worker.py`，预检与正式 worker 分别登记；先落创建意图和完整 ID，启动前持原设备锁核对并落意图，再释放锁让 worker 自行取得。停止仅操作原 ID/契约/启动时间，不持队列行锁等待，也不能删除任务记录或释放预约。已登记对象丢失或同名替代不得重建认领；未确认的生命周期须先恢复再发布配置。按[生命周期检查](docs/deployment/container-gpu-worker.md#worker-lifecycle)区分真实文件/进程与 Docker 模型，后者不能证明镜像、数据库或 GPU 部署成功。
- 发布就绪观察在 `scripts/deploy_gpu_readiness.py`：固定运行时声明只从已核验归档读取字面量，不执行归档代码或借当前工作树补旧制品；核对原容器启动、完整环境、新鲜心跳及全部任务/预约。环境核验后再观察原实例和最终数据库快照，成功不能缓存为曝光凭据。等待不持设备/队列行锁，不延长原启动预算；超时保留原对象，检查入口见[就绪观察](docs/deployment/container-gpu-worker.md#readiness-observation)。
- 原库 worker 恢复在 `scripts/deploy_gpu_recovery.py`：只恢复当前私有配置所指向的原代，曝光后只允许当前代；先保存意图，再停止原实例、执行新预检、启动同一容器并重查就绪。预检必须属于当前操作且开始不早于上一 worker 的实际退出时间，不能跨停止重用。未完成恢复阻止配置发布；不能先换库、清预约或删 journal，公共 CPU 路径不能接管 GPU 状态。按[原库恢复检查](docs/deployment/container-gpu-worker.md#worker-recovery)验证中断与重试，模型结果不算真实 GPU 恢复。
- GPU 候选队列检查在 `core/deployment_gpu_check.py` 和 `scripts/deploy_gpu_check.py`，固定程序只能经普通准入与原 GPU worker 执行。保存原启动/操作/指纹，超时保留原提交且不刷新期限；成绩通过后先持久确认，再单独清理，响应丢失不能重新入队。清理仅删除精确自有、全部终态且无任何物理预约的测试记录。不得把清理成功或持久 verified 阶段当作新启动就绪。新增应用源码会改变 worker 契约，旧包核验不授予新命令能力；按[候选检查入口](docs/deployment/container-gpu-worker.md#candidate-verification)区分实际数据库/文件/进程与合成设备、成绩及 Docker 模型。
- GPU 原代停止/冻结在 `scripts/deploy_gpu_quiescence.py`：绑定完整发布代和服务名，先排空再停止原 GPU worker；等待停止不持设备/队列行锁，确认退出后持原设备锁复查 journal 和全部登记对象，再停止 Web/CPU worker。`freeze_gpu` 须持排空事务直到 CONNECT 撤销和 runtime 会话终止，仅暂时允许本次精确 owner 后端，事务关闭后不允许任何目标库会话。重试重新检查，不缓存排空、不自动解冻或清预约；按[冻结检查](docs/deployment/container-gpu-worker.md#database-freeze)分别验证模型、实际文件/进程和 PostgreSQL。
- 界面字符串使用 Django 翻译，维护 `.po` 和编译后的 `.mo`；内容译文绑定准确的源版本。缺少译文明确回退，不能借用另一版本文本。
- GPU 候选协议 v2 将轮次 UUID 与暂停 token 分开；新启动只可在原轮 verified/cleaned 后建立新轮，同一操作/启动不得换轮刷新预算。恢复清理继续用旧轮 UUID，不能以新轮跳过待恢复任务；达到上限保留历史并拒绝继续。冻结源码协议与私有 schema 必须匹配，不改写旧记录，见[轮次决策](docs/adr/068-gpu-candidate-verification-rounds.md)。
- 连续候选准备在 `scripts/deploy_gpu_candidate.py`，绑定完整代、原操作与规范归档路径，持久串接配置/服务/预检/启动和两类队列检查。恢复先处理原库 CPU/GPU 工作、核对原恢复记录，再清理旧轮和建立新尝试；不得先切库或跳过失败清理。verified 仍须重查 CPU/GPU 就绪以及轮次的设备/运行契约，不是曝光许可；检查见[候选准备](docs/deployment/container-gpu-worker.md#candidate-preparation)。
- GPU 曝光在 `scripts/deploy_gpu_exposure.py`：恢复准入前原子保存当前库及曝光决定，之后不回到旧快照。逐次核对原准备轮次、配置、启动、owner、环境和端口；开放后的合法任务由冻结应用的正式准入校验，不要求清空用户队列。再次暂停或撤回路由须经原库恢复和新轮验证；历史决定保留，不能据旧记录重开新实例。普通生命周期仍要求暂停和维护路由，检查见[持久曝光](docs/deployment/container-gpu-worker.md#durable-exposure)。
- GPU 配置退出在 `scripts/deploy_gpu_retirement.py`，只允许本操作曝光前、原库冻结与排空、全部 worker 停止及原设备锁同时成立时执行。保存原快照后经同一过渡标记有序移除匹配配置对；底层也要重查退出记录，未完成退出阻止发布。保留节点、锁、数据库和全部历史，不以 enabled=false 或手工删文件替代；检查见[配置退出](docs/deployment/container-gpu-worker.md#configuration-retirement)。
- 内部整站编排在 `scripts/deploy_gpu_release.py`；恢复方向取原曝光决定，曝光前先处理实际配置所在原库的工作再回退，曝光后保留当前库与新写入。保留规范归档、每次恢复阶段和所有历史；未知或未标记数据库不得按不存在处理。曝光响应丢失时不重启仍运行的原 worker，确认原实例退出后才重做原库恢复和验证。模型通过不解除公共 GPU 激活门槛，检查见[整站发布与恢复](docs/deployment/container-gpu-worker.md#whole-site-release)。
- 编辑器源码在 `frontend/`，按 [编辑器指南](docs/editor.md)更新 npm 锁文件、预构建静态资源和许可证；不能只改源码而遗留旧 bundle。保留 textarea 回退、私有草稿、异步修订保护及 CSP nonce，分析工具不得执行源码或外发代码。界面改动用 `scripts/browser_acceptance.py --interface` 验证中英、多种宽度、键盘操作与失败回退。
- 标题、列表、表单与选项须共用页面边距。无独立页头的内容页继承 `workspace_base.html`；带 `.page-heading` 的正文使用 `.section.compact`，避免正文直接落在无边距的 main 下。布局检查应比较正文和标题的实际起点，并包含 320 像素手机页、多行标题及两种 Django 复选框结构，不能只检查横向溢出。账号说明、长笔记、连续卡片和令牌选项也要覆盖；比较栅格容器时计入其内边距，卡片间距规则不能改变多列布局。表格、卡片之后的下一节标题须留出间距，检查实际上下边界，不能仅凭左右对齐判定排版正常。
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
- 完整迁移 schema、角色权限、冻结/解冻和实际数据库复制使用 `tests/test_deploy_database_postgres.py`，沿上述 runner 提供的自有容器身份运行。它核对容器标签和回环端口，为每项检查建立随机数据库/角色，按原标记清理；不能把最小 schema 或 SQLite 结果当作完整 PostgreSQL 恢复验证。集中入口见[数据库验证](docs/deployment/production.md#database-validation)。
- 历史 F schema 升级使用同一 runner 的 `tests/test_deploy_schema_upgrade_postgres.py`。冻结清单在 `tests/fixtures/f_migration_history.json`；先核对历史迁移、内容字节和 Django 版本，再用历史模型在自有空库建立合成数据，经真实复制及当前迁移逐列比较。不能改写旧迁移来满足摘要，也不能把重建的历史数据库当作旧应用镜像到新版本的整站验收。
- F→当前 CPU 整站验收由 `scripts/deploy_current_acceptance.py` 接入原 `deploy_acceptance.py --upgrade-only`，操作见[集中升级检查](docs/deployment/production.md#f-current-upgrade)。只接受冻结 F 清单和明确新增的三项迁移，不把校验放宽为任意后继版本。原分享字段投影须另验 `metric_kind=sequential-v1`；保留原 CPU 结果/预约、会话和私有数据，GPU 表保持空。组件和当前包通过不能替代实际运行原 F 应用，也不解除此前旧缓存导入的审批限制。
- 真实评测先按 [WASI 指南](docs/deployment/wasi.md)配置可信 bundle、摘要及 `JUDGE_RECEIPT`，实际运行 `sandbox_check`。`RUN_SANDBOX_TESTS=1` 才启用相关集成场景；记录跳过原因，不把 skipped 当通过。
- GPU 基础环境检查使用 `scripts/gpu_environment_probe.py`，按 [GPU 指南](docs/deployment/gpu.md)显式选择维护者授权的空闲单卡。检查采用固定完整 runsc 归档、临时用户服务和独立存储；不能以 CLI 参数代替真实父 cgroup 限额和节流检查。可选 `--nvrtc-archive` 重放审阅过的内核参考解/错解，编译也必须在隔离内进行，受保护答案留在控制器。各模式串行执行，不生成正式评测 receipt；保留制品/源码身份、失败、最终结果和清理记录。
- GPU `--fault-probes` 只用于明确授权的独占卡，客户端超时后必须确认自有容器终止及设备无残留，并重跑正确解；不能终止未知进程或自动重置 GPU。`--image-archive` 可减少下载依赖，但必须校验固定清单与完整镜像层后导入独立存储，继续实际探针。CPU 内存配额、设备堆限制和整卡显存容量不能相互替代。
- GPU 调度在 `core/gpu_resources.py`，主机设备锁在 `judge/gpu_device_lock.py`。所有控制器副本须共用私有锁目录，不能删除锁文件；设备行先于任务行加锁，拿到锁后重新核对租约。超时、账号删除及部署恢复不能自动释放设备预约，只有持锁控制器完成真实清理与恢复后才能确认可用。调度测试不替代硬件检查，CPU 调用额度与 GPU 整卡时段预约分别记账。
- GPU 网页准入在 `core/gpu_admission.py`；`JUDGE_GPU_DEVICES` 默认空。Web 只读完整启动发布的环境、当前策略与新鲜设备状态，不读私有凭证或取得设备行写锁。设备换所有者时清空发布信息，旧迁移记录不能自动启用；排空必须包括所有持久设备预约，删除账号、结束任务或移除配置不能隐藏它们。改动需重放实际 PostgreSQL 并发、最小权限、发布事务和升级检查；合成环境不能用于实际开放提交。
- GPU 交互协议与控制器比较在 `judge/gpu_session.py`、`judge/gpu.py`；`--session-probes` 复用实际会话路径。管道夹具不证明 CUDA 或性能成功；GPU 性能只能使用控制器测量，不采纳 guest 的计时字段。文件锁不隔离其他账号的 CUDA 进程，发现外部占用时停止启动并等待独占条件，不能用低利用率代替空闲证明。
- GPU 服务监督在 `judge/gpu_service.py`，短事务回调在 `core/gpu_control.py`；仅可信控制器可传入启动命令，源码/答案/密钥不得放进命令行或日志。终止前核对预约及服务调用身份，终止后核对整个 cgroup；数据库等待失败不直接释放设备。服务控制改动用 [非 GPU 控制链检查](docs/deployment/gpu.md#不占-gpu-的控制链检查)和实际 PostgreSQL 验证；固定 CPU 进程树通过不替代 CUDA 清理、设备空闲与正确性恢复。
- 容器监督基础在 `judge/container_service.py` 和 `judge/container_entry.py`。仅可信程序可使用固定镜像，保留私有 rootless socket、空白客户端配置、最小环境、绝对期限和实际容器实例核对；不能把 runc 容器用于学员代码。按[集中检查入口](docs/deployment/gpu.md#container-supervision)重放固定 CPU 故障及清理；同名替换或无法确认停止时不得清空预约，不以此检查生成 GPU 凭证。
- 容器内 runsc 使用 `judge/container_runtime_service.py` 和 `judge/container_oci.py` 的独立配置及身份范围；按[嵌套运行时入口](docs/deployment/gpu.md#container-runtime)验证。固定 Moby 配置及七项额外调用不能改成 unconfined；完整运行时归档须先校验，外层代码和状态不得进入 guest。外层总配额与 guest 子配额必须分别说明；此开发路径不生成 GPU 凭证，未接入正式提交。
- 容器控制通信在 `judge/container_connection.py`、`judge/container_transport.py`；只对已持锁、完整配置核对后的容器 ID 启动固定桥接器，原启动握手通过前不发送任务。容器内关闭连接后须在原期限内通过 pidfd 等待原桥接进程退出，再结束主控制器；不能忽略 Docker exec 非零退出码或延长期限。不要加任意 exec、主机 socket 挂载或环境继承；断连不能重连续跑，关闭 CLI 不等于 guest 清理。按[通信验收入口](docs/deployment/gpu.md#container-transport)实测背压、错配、断连、取消和独立期限；不使用 GPU。
- 持久容器身份在 `judge/container_journal.py`。先记录意图、再创建，启动前持久保存 ID；记录绑定私有安装身份、同一 daemon 和锁目录，不能靠当前 UID 或新源码认领旧任务。恢复只停止/移除已保存契约匹配的对象，归档前保留活动记录，不执行旧镜像、不直接释放数据库预约。修改后按[持久恢复入口](docs/deployment/gpu.md#container-journal)重放真实中断、旧制品和同名替换检查。
- 容器化可信控制端在 `judge/container_controller.py`。镜像内 UID/GID 0 必须映射到原非 root 操作者，按私有清单核对同一 socket、锁和 journal inode；不能改成宽松文件权限、挂整个用户目录或用第二套锁绕过竞争。Docker socket 只能给可信控制端，不能进入 Web、任务容器或 guest；只读 socket 挂载不限制 daemon API 权限。修改后按[控制端映射入口](docs/deployment/gpu.md#container-controller)重放真实 CPU 双向锁与跨容器接管，不生成 GPU 凭证。
- 容器内宿主进程归属在 `judge/container_processes.py`，只在显式挂载契约 v2/v3/v4 下启用；保留 v1 无宿主 procfs 的语义。可信控制端核对 procfs dev/inode、boot ID、递归只读和 rprivate，固定读取 stat/status/cgroup；不把 `..` cgroup 文本当作路径，不向任务或 guest 暴露挂载。归属绑定原 journal、容器 ID/启动实例、PID 1 出生时间和实际 cgroup；观察失败不等于设备空闲，不停止未知 PID。按[进程归属检查](docs/deployment/gpu.md#container-process-observer)及原控制端回归实测；CPU PID 证据不能替代 NVIDIA 进程来源与 CUDA 恢复检查。
- 容器 NVIDIA 管理查询在 `judge/container_monitor.py`、`judge/nvml_reader.py`、`judge/smi_inventory.py`。完整来源使用 v4 契约和管理制品 v2，同时固定 NVML 与 SMI；完整 XML 列表不按进程类型过滤，三类上下文必须是其子集，缺失、部分失败、来源冲突或超时不等于空闲。原 v3 仅保留上下文观察，不能自动升级为完整来源。只映射所选卡与控制字符设备；`nvidia-caps` 只能是私有 `/dev` 中的空目录。沿用同一原任务观察对象，不能重新认领 PID，也不输出原始 XML 或进程名称。按[只读管理检查](docs/deployment/gpu.md#container-device-observer)重放并保留旧契约兼容；没有实际 CUDA/其他/无上下文进程时记录证据缺口，不能擅自启动 GPU 程序补造证据或签发就绪凭证。
- 数据库与容器清理绑定在 `core/gpu_container_recovery.py`，已通过可选 journal 接入宿主 worker 的启动、续检和取消恢复。先停止原对象，归档前后核对隔离状态、拥有者与原预约；DB 锁失败也保留可重试记录，不能释放额度或借用其他任务。维护身份统一由 `core/gpu_identity.py` 生成。修改后重放[数据库/容器联合检查](docs/deployment/gpu.md#container-database)，并验证旧宿主生命周期；CPU 清理结果不能签发 GPU receipt。
- 单卡任务配置在 `judge/container_devices.py`、`judge/container_gpu.py`；journal v3 保存同一 UUID、驱动、minor、UVM 注册与总预算，v1/v2 保持无设备原义。恢复只按冻结映射停止原对象，不能把当前镜像或设备配置套给旧记录。嵌套 runtime 的节点检查不证明 CUDA 或 ioctl 隔离；[设备节点检查](docs/deployment/gpu.md#container-device-nodes)仍会映射真实设备，需要明确授权，“不占 GPU 的开发”不能自动视为允许此检查。保持只读根、私有命名空间和原 seccomp，不因挂载失败放宽权限。
- 容器内核调用在 `judge/container_execution.py`、`judge/container_kernel_flow.py`。准备身份、开始回调与原记录核对后才传源码；结果必须晚于任务 guest 清理、新 guest 正确恢复、外层原记录移除和空闲观察。原硬期限内的清理不能依赖已断开的通信回调，只能观察或删除原 guest。按[无设备调用链检查](docs/deployment/gpu.md#container-kernel-flow)重放 CPU 协议和取消边界；其固定 CPU 程序不编译源码，计时和结果不能作为 CUDA、正式环境、receipt 或 worker 验收。
- 容器环境与执行适配在 `judge/container_environment.py`、`judge/container_adapter.py`，结算复用 `core/gpu_execution.py`。独立环境绑定整任务预算与镜像/驱动/管理/引擎身份；开始回调后再次核对，失败只清理原任务对象。控制镜像须携带完整 `EXECUTION_SOURCES`；历史成绩不能与当前源码比较或冒用宿主格式，不能用有效历史记录替代就绪检查。
- 容器固定内核检查在 `judge/container_kernel_checks.py`，复用原 CUDA 目录和判定规则，仅接受维护目的、固定名称及随机挑战。嵌套 guest 使用独立清理报告，保存原状态与附着退出码；中断前须另行核对原 guest/附着进程活跃，事后已停止不能替代该观察。重复清理不能覆盖首次证据，私有标签不能冒充宿主容器 ID。修改后重放[无设备调用链检查](docs/deployment/gpu.md#container-kernel-flow)；CPU 夹具不能证明 CUDA、显存故障或完整启动通过。
- 容器资源启动检查在 `judge/container_isolation.py`，报告由 `container_isolation_protocol.py` 按原请求、目标和设备选择校验。九项检查及十七个新 guest 保留顺序、测量值与原清理证据，之后还须独立正确恢复。GPU 逻辑序号不能冒充物理 minor，任务回环不能冒充宿主回环；CPU 模式不得加载驱动或查询设备。使用同一[无设备调用链入口](docs/deployment/gpu.md#container-kernel-flow)重放资源成功和后续恢复失败；整个任务 OOM 或断连不得作为资源报告成功，也不能生成 GPU receipt。
- 容器控制器故障在 `judge/container_lifecycle.py`，固定握手与宿主共用 `controller_faults.py`。只有固定正确程序通过比较、原 guest 活跃后才能触发；独立期限只能观察，不能主动停止来造证据。断连须保留仍活跃的原任务，再按 journal 清理；新正确恢复须换容器 ID 和 nonce，保留原锁/任务/预约和时间预算。按[无设备控制器故障入口](docs/deployment/gpu.md#container-controller-faults)核对主动清理前的原进程树消失、失败扣留和新恢复，不将其当作 CUDA 或完整启动凭证。
- 容器完整启动由 `core/gpu_readiness.py` 调用无数据库依赖的 `judge/container_startup_suite.py`；初始恢复在 `core/container_startup.py`，独立报告与私有身份在 `judge/container_readiness.py`。共用流程须校验调用者刚完成的原恢复，并执行全部后续检查，只返回完整证据；数据库预约、凭证与就绪事务仍由服务端负责。本地调用者不能导入旧报告跳过恢复，也不能借此获得 ready。原 journal、任务/预约和完整设备观察均须匹配；未完成的宿主任务不得重新标记为容器指纹。schema 2 凭证绑定实际宿主和安装身份，不能用容器 UID 0 冒充宿主 root。修改后按[容器启动组件检查](docs/deployment/gpu.md#container-startup)验证独立导入、失败/取消边界、实际 PostgreSQL 发布竞争与 Web 只读权限；测试替身和临时文件不证明 GPU 可用。
- 专用容器入口为 `container_gpu_worker`，部署统一见[容器 worker 指南](docs/deployment/container-gpu-worker.md)。固定镜像先核对完整应用源码，再从原私有状态读取配置；不继承外部 Django/数据库环境。最小权限检查复用 `scripts/deploy_database.py` 的授权定义，保留列权限和转授拒绝。`gpu-check` 不访问设备或发布 ready；只有正常 worker 才执行完整启动。新增应用 Python 文件须核对 worker 镜像源码范围；修改后重放实际无设备镜像/Compose/PostgreSQL 入口及原控制端恢复。`--initialize` 会查询设备元数据，需要已有授权，不能因为无设备验收通过便自行执行。
- Web 基础镜像也须携带完整 GPU 执行摘要源码。新增策略文件时核对 `Dockerfile` 的复制范围，保留构建时纯文件检查，以及默认 Compose 首次启动、重建后 Web/CPU worker 与源码摘要的一致性检查；这些检查不查询 GPU，也不代表设备 ready。
- 无设备 CUDA 编译与正式 runtime 共用 `gpu_runtime.compile_ptx`，只在经过验证的 gVisor guest 中处理源码。固定编译镜像使用独立契约及经过摘要校验的 NVRTC 库，不加 GPU、驱动、宿主挂载或宽松 seccomp。修改后重放[无设备编译检查](docs/deployment/gpu.md#container-compiler)，覆盖语义错解也能编译、错误分类及编译后清理；PTX 元数据不是正式成绩，不能据此发布题目或签发 GPU receipt。
- 容器内的可信编译调用在 `judge/container_compilation.py`，只使用原 journal 和版本化总预算。控制镜像须包含完整编译源码契约，不能借用宿主摘要或解释器；开始回调和原记录核对成功后才发送源码，结果须等原任务清理后返回。保留取消、平台故障与编译错误的区别。修改后重放[容器编译控制检查](docs/deployment/gpu.md#container-compilation-control)及原控制端接管；编译身份不是正式 GPU 环境，接入正式执行时须再纳入对应策略与完整启动检查。
- 本地 `compile` 与 `recover-compiler` 在 `scripts/practice_cuda.py` 复用上述无设备编译调用。状态绑定原目录、锁/journal inode、安装身份和 daemon；逻辑锁 UUID 只代表编译工作区。公开包不能控制镜像、路径或设备，旧活动记录须先恢复；不得接管无标记目录或删除原记录。按[本地编译指南](docs/deployment/local-compiler.md)重放真实 CLI、SIGINT/SIGKILL 和原任务清理。编译成功只表示 PTX 生成，不计公开样例、正式成绩或 GPU 就绪；新模块随源码包交付，不为 Web 增加 Docker 访问。
- 本地 GPU 内部生命周期在 `judge/container_practice.py`，原物理状态绑定在 `container_practice_state.py`；初始化只接受未使用的原 v4 状态，不新建替代锁或接管服务端预约。操作先登记，再完整启动和执行公开任务，结果晚于清理、终态持久化及最后取消/身份核对。恢复只处理原操作的冻结任务，不使用新镜像、不发布 ready；worker 与协调器必须拒绝本地状态。按[容器启动组件检查](docs/deployment/gpu.md#container-startup)核对真实文件/SIGKILL和模型边界；外部 CLI/通信与硬件验收未通过前，不能宣称完整本地 GPU 工具交付。
- 本地固定控制入口为 `scripts/container_practice_entry.py`，协议和有界管道在 `judge/container_practice_protocol.py`、`container_practice_transport.py`。先核对原状态握手与完整环境，再发送公开源码；逐次执行完整启动，不接收客户端凭证。身份比较须区分布尔值和整数，发送源码前和返回结果前显式检查取消。恢复不构造执行适配器；宿主启动器还须核对原容器退出与清理，不能仅凭完成帧/EOF 返回成功。设计见 [ADR-077](docs/adr/077-local-gpu-controller-protocol.md)。
- 本地宿主状态在 `judge/container_practice_host.py`：客户端互斥只串行化命令，控制容器仍取得原物理锁。先绑定原 v4 目录、安装、socket、daemon 与标记，再持久保存外层记录；记录固定客户端锁 inode，旧记录变化时拒绝覆盖。恢复外层控制容器时不能先争用它持有的物理锁，不能替换缺失锁、删除内部任务或把清理当作 ready。设计见 [ADR-078](docs/adr/078-local-controller-host-ownership.md)。
- 本地原控制容器在 `judge/container_practice_controller.py` 登记；同时核对原 ID、名称、创建时间、冻结配置和启动时间。重开旧意图只能清理，不启动旧镜像；移除响应丢失也须有先前的原移除意图。按[宿主生命周期检查](docs/deployment/gpu.md#local-controller)重放真实 CPU 容器，测试入口和合成设备仅限验收夹具，不能加入生产无设备回退或代替固定 GPU 入口的完整协议/退出验收。
- GPU 本地公开测试使用 `execute-public`，仅接受公开用例并固定 Run；不能补造受保护样例或放宽正式 `execute`/Submit 校验。响应须带布尔 `formal_result: false`，不得带性能成绩或混用正式完成响应；结果仍晚于独立恢复及原容器清理。协议/适配器接口不替代本地完整启动、物理锁和持久任务恢复。见[公开任务决策](docs/adr/074-public-only-gpu-task-protocol.md)，修改后重放集中指南的 CPU/runsc 调用链，明确它不是 CUDA 实测。
- 本地 CUDA 驱动制品在 `judge/driver_artifacts.py`、`judge/container_driver.py` 与 `scripts/build_container_driver.py`。只从不可由普通用户改写的系统目录静态复制和检查库；清单、源码及镜像身份共同绑定，升级后重新构建。按[驱动制品检查](docs/deployment/gpu.md#container-driver)在无设备 guest 中验证加载、编译与清理，不在宿主加载库，不调用 CUDA 初始化，不把加载通过当作 PTX JIT、设备隔离或 GPU 就绪。四库配置不自动覆盖其他 CUDA 功能；保持原 journal 契约及编译镜像的无驱动边界。
- 容器资源配置在 `judge/container_contract.py`：`container-workload-v1` 限制整个任务，含监督器和运行时开销；不等同于 guest 子配额或 GPU 显存。原开发配置和 journal v1 保持冻结，新预算使用 journal v2。OOM 分类须观察本 cgroup 的超限事件，并先确认原 guest/进程树清理；不能只靠退出码或全局 OOM kill。固定宿主 PID 探针只在核对过的私有任务容器内运行，不接受源码或命令。修改后重放[任务预算检查](docs/deployment/gpu.md#container-workload)及两种记录的数据库恢复，不复用旧 GPU receipt。
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
- 浏览器、HTTPS、发布包和升级用现有 `scripts/*acceptance.py` 入口。使用一次性数据库、合成账号和明确属于本次测试的容器/卷；默认使用回环地址，按用户明确要求验证直接 IP 访问时使用独立测试端口。完成后核对清理，不删除未知资源。源码浏览器通过 `BROWSER_ARTIFACTS` 分开保存每轮日志、截图和报告；旧发布包的编辑器契约由已验证的当前代清单决定，新版仍须验证 CodeMirror，不能用自动 textarea 回退掩盖加载失败。
- 无头 Chromium 统一通过 `tests/browser_runtime.py` 启动，只对浏览器子进程移除显示连接与 D-Bus 会话环境，保留调用者环境、缓存路径及原 TLS 参数。长时验收前运行 `python -m tests.browser_runtime`，实际截图成功才算渲染就绪；不能仅凭浏览器启动成功、延长超时或跳过截图宣称通过。发布验收的预检失败须在 Docker 操作前结束，并清除本轮入口的旧成功摘要。
- Compose 单机入口为 `core/selfhost.py`，复用 `core/preview.py` 的原子数据库初始化；默认包括真实 WASI worker，可选 `compose.ai.yaml` 启动本地模型。修改时重放 `python3 scripts/compose_acceptance.py`；涉及模型或界面时使用 `--ai --browsers`。核对真实探针与七种题型、worker 离线关闭提交、直接 IP/CSRF、初始化恢复、旧内容不重导及成绩/会话跨重建保留。不得用旧 receipt 或宿主缓存代替镜像内自检。此入口不替代生产发布协调器。
- 重型 WASI/worker 验收按资源预算串行安排，避免争用造成假超时。长进程须跟踪原进程或工具句柄；观察超时不代表进程结束，不据此重复启动。
- 构建和验收的镜像清理仅操作自有对象，包括失败路径的 `finally`；删除临时镜像使用 `--no-prune`，并核对原固定基镜像仍存在，避免 Docker 连带清掉未打标签的原镜像缓存。不得覆盖原发布归档或未知镜像标签。构建脚本若参与冻结源码契约，修复清理逻辑后也须重建匹配镜像，不能沿用旧身份。
- 托管 CI、部署 runner 与 GPU 评测 worker 分别验证；接入步骤见 [CI 指南](docs/deployment/ci.md)。自定义 runner 标签不构成权限隔离，公开 PR 不能接触持有站点权限的 runner。GitHub 管理 API 查询失败不得当作 runner 不存在；部署 `status` 只验证记录读取，不证明站点健康。
- 保留原始失败和修复后的结果，记录代码/策略/制品身份。结果文件放忽略目录 `test-results/`，必要的结论写入对应验收文档；不以单元 mock 证明真实隔离、GPU、模型或部署成功。
- 文档变更核对链接、命令参数和当前行为。初始化、导入和恢复示例尽量在临时副本/数据库验证，避免改变开发者已有数据。

## 文档与 Git 提交

- 本地 GPU 公开命令经 `scripts/practice.py` 和 `judge/container_practice_client.py` 调用原控制器，操作统一在[本地 GPU 指南](docs/deployment/local-gpu.md)。只有协议、原进程/容器成功退出和内部终态同时确认才返回结果；中断沿原物理状态恢复，不重放源码。`build_local_gpu.py` 仅静态构建，`prepare-gpu`/`initialize-gpu`/`test-gpu`/`recover-gpu` 均可能访问设备，不得因“不占 GPU 的开发”而执行。CPU 管道验收与镜像构建不能证明硬件通过。

- README 面向首次使用者，说明当前能力、快速体验、真实评测、自部署与开发入口；历史执行细节放对应验收文档。保持需求追踪、RFC/ADR、使用指南和 CHANGELOG 与实际行为一致。
- 部署操作统一维护在 [docs/deployment/](docs/deployment/README.md)：首页负责方案和场景选择，专页负责对应操作。README 只保留最短启动入口，旧文档路径只做跳转；新增部署选项时同步更新选择表，避免在多处复制完整步骤。
- **工作过程中及时 git commit**：每完成一个可审阅的相关增量，就记录具体问题、变化和验证结果。提交前检查差异，只包含本次任务文件；未完成或未验证部分必须如实说明。用户未要求时不推送、不改写既有共享历史。
- 不使用 `git add .`、reset、clean 或 stash 来掩盖混合工作区。已经存在的用户暂存内容应保持原状。
- 本项目开发工作区可能仍保留未出生的 `master` 和用户暂存的 `PRD.md`，而实现历史保存在 `codex/full-product`。遇到这种状态，先用 `git log codex/full-product` 核对，不能把所有 untracked 文件当成没有历史。沿该审阅分支追加提交，使用临时索引或隔离工作树，保持原 HEAD 和索引；不要为方便提交擅自切换或重置用户工作区。正常检出的分支按其实际状态提交。
- 不提交 `.env`、密钥、恢复码、数据库、真实用户代码、运行凭证、缓存或测试截图。临时日志可能包含敏感内容，保存和分享前检查。

## 尚未具备的外部条件

GPU 题型、托管 CI/部署目标与公开运营/授权事项以[需求追踪](docs/requirements.md)为准。没有经过验证的 GPU 隔离、资源预算和真实正确性/性能检查时，不开放 GPU 提交。不要自行修改共享 daemon、驱动、主机服务或批准项目许可证；在已有授权内完成可逆开发，新增外部运营决定由维护者明确。
