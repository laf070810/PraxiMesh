# 独立临时站点的 GPU 发布验收

更新：2026-11-15。**GPU 网站部署尚未验收通过，公共激活入口仍关闭。** 本页记录 [RFC-026](rfcs/026-gpu-site-hardware-rehearsal.md) 的真实临时站点检查；[本地完整 GPU 启动与公开样例](acceptance-gpu0-startup.md)已有独立通过结果。

## 首次 CPU→GPU 尝试与原站点恢复

采用源提交 `75e2762` 的[同源 CPU/四镜像包](acceptance-current-releases.md#uvm-release)，新建 PostgreSQL、回环 HTTPS、合成账号和独立 GPU 节点。原 CPU 版本的实际提交、草稿及目录快照通过；普通 GPU worker 创建完成但未启动。

固定无设备预检实际退出码为 0，却被协调器的创建摘要核对拒绝。完整配置仅有 Docker 29.9.2 的两项正常启动变化：OOM 分数 `0→200`，OOM killer 禁用字段 `false→null`。两字段的精确逆变换重现原完整摘要；原发布操作、数据库、容器和节点均保留，没有另建站点绕过。

[RFC-027](rfcs/027-rootless-worker-oom-contract.md) / [ADR-081](adr/081-rootless-worker-oom-contract.md) 的修复提交为 `645f0dd`：新创建时固定 daemon 的有效 OOM 设置；旧预检只追加退役证明。472 项相关回归通过；真实恢复首次又发现预检在正常首次启动前曾执行停止，因此原 `started_at` 是零时间而非空值。保留该失败，补齐两种从未启动状态后，72 项定向检查通过。

随后从**同一个发布操作**恢复成功：原预检及从未启动的 GPU worker 停止、原 GPU 配置退出、原 CPU 库/版本恢复、回环 HTTPS 健康通过，操作终态为 `rolled_back`。耗时 346.330 秒；最终 GPU 0 空闲，已有 Web、CPU worker、tutor 的完整身份、启动时间及健康状态与恢复前一致。临时站点保留，供后续验收使用。

私有证据在 `test-results/uvm-releases-2026-11-15/`：`owned-gpu-site-1/`、`legacy-preflight-recovery-1/`、`legacy-preflight-recovery-2/`。恢复后报告保存原/新发布快照和两次服务观察；原失败不覆盖。此结果证明该次曝光前失败的真实恢复，不证明完整 GPU 发布故障矩阵。

## 包含 OOM 修复的新冻结包

源提交 `645f0dda0f4f961348c8f6eb7b4e7e55393172b1`，992 个文件逐字节匹配 Git，source ID `e65c07547ffe58cb7634cb14a07138e30e5900eefffe7e32c400b0833dea3aae`。

| 格式 | 不可变目录 | release.json SHA-256 |
| --- | --- | --- |
| CPU | `var/releases/2026-11-15-cpu-upgrade-target-6/` | `816d2cc230395d2d6220d092c2832fdfff712eae9f50407757c9c8dc06d520e8` |
| GPU | `var/releases/2026-11-15-oom-four-image-1/` | `622e36b4a968115e4f286b9662c35c0327e79c993adce7f001595de47b3e84bc` |

两包同源、Web/CPU 镜像和评测配置相同；GPU worker 与 task 镜像沿用完全相同的已核验字节。完整核验、标准导入及 10 项无设备 worker/Compose/PostgreSQL 检查通过。CPU 应用首轮通过启动探针与部分浏览器流程后，因调用者未提供本地 CLI 所需的匹配 WASI 环境而停止；补齐并提前校验 bundle 后，在新目录完成完整重放，433.774 秒、退出码 0。129 项启动探针、三组基础/七组扩展浏览器、实际提交与资源清理均通过，报告为 `process-final.json` 和 `cpu-application-2/release-acceptance.json`。

GPU 构建在完整制品保存后，清理唯一临时镜像标签时失败：原预检仍引用同一 GPU worker 镜像。原制品不重写，独立核验后继续；临时标签及镜像保留。构建清理已补充按完整 ID/标签及实际容器引用检查，使用中的最后一个标签保留并写入独立 `cleanup.json`，其他标签仍使用 `--no-prune`。98 项相关组件检查通过，实际保留路径确认原镜像元数据未变；没有停止引用该镜像的容器或强制删除缓存。

新包及构建原始失败在 `test-results/oom-releases-2026-11-15/`；`source-and-pair-audit.json` 保存源提交与镜像身份，`cleanup-reproduction.json` 和 `cleanup-safe-retention.json` 保存实际标签检查。源码包本身仍是原 `645f0dd`，不包含随后补充的构建清理逻辑。

## 正式 worker 的首次完整启动

原临时站点继续采用这批冻结包，新 schema 2 无设备预检通过，正式 GPU worker 启动成功。首次启动及随后原操作恢复均完成 21 个阶段：初始恢复、资源检查和全部 19 项内核检查，包括实际超时、非法地址、取消及附着进程终止。

两次均在首个控制器故障检查失败，后两项未执行；没有签发就绪或开放提交。第二次原任务的 Docker 事件确认独立退出码为预期的 70，但完整检查仍未通过，具体失败条件待定位。现场为 `owned-gpu-site-2/`，最小事件观察仅保存容器 ID、时间和退出码，不保存命令/环境内容。原临时库、配置、容器和任务历史继续保留。

此时原 CPU 回退也等待同一 GPU 完整启动，因此新增[有限中止路径](rfcs/028-failed-gpu-startup-cpu-rollback.md)。300 项相关组件、补充后的 26 项边界检查及 39 项真实 PostgreSQL 冻结/配置检查通过；临时 PostgreSQL 资源已清理。原站点只读资格预检通过，发布日志字节未变。

首次实际中止已确认原 worker 停止并冻结候选库，随后配置退出因自己的合法 owner 检查事务触发零会话校验而保留现场。补充精确 guard backend 作用域后，40 项真实 PostgreSQL 与 178 项组件回归通过；额外及后续连接仍拒绝。原报告为 `failed-startup-cpu-recovery-1/`。

同一原操作随后完整回退通过，346.884 秒；原 CPU 库/版本恢复，GPU 配置退出、设备空闲，原准备和恢复记录保持不变。独立核对原账号、草稿、CPU 提交、目录摘要及序列通过，三项原有服务未变。证据为 `failed-startup-cpu-recovery-2/result.json` 和 `data-preservation.json`。

worker 失败日志现只增加最多四个已冻结平台源码文件/行号，不输出异常消息、源代码、局部变量或外部路径。112 项诊断/worker/制品组件检查通过；该改动改变执行源码身份，后续硬件检查必须使用新匹配镜像，不能沿用本批包作为新版本通过证据。

## 新诊断版本与后续部署

提交 `ccf07eff47e6760b15446066b18fddc6c1698964` 的 999 个源码文件逐字节匹配 Git，同源 source ID 为 `4e9e71dc33a0dd450f3c3623fcfa5de3ce949b7d6ba37aee9717e2002b6bf57e`。CPU 包 `2026-11-15-cpu-upgrade-target-7` 的清单摘要为 `6060519cbb8e6b6d082fcc45dd64e19d2d8f8c1e588535b2aba5103d8d91a061`；GPU 包 `2026-11-15-worker-location-four-image-1` 为 `b0789bee43448b7804867eaa6654dd3e6193ae17f31ca1783b940f8ac55f4b95`。

两包构建、归档核验、导入及 10 项无设备 worker 检查通过。CPU 应用完整检查用时 440.141 秒，129 项探针、三组基础/七组扩展浏览器、真实提交及清理通过。结果保存在 `test-results/worker-location-releases-2026-11-15/`，这些结果不证明该新执行身份的 GPU 启动通过。

再次部署在启动 GPU 前被旧中止证明拒绝：整站完成回退后内部阶段为 `active`，而新增证明只接受恢复中状态。原历史摘要全部一致。修复按完整整站记录、最后一次已完成恢复及原 CPU 目标核对，不改写历史；新增拒绝错误目标、无恢复记录及未完成恢复的检查，66 项相关测试通过。原始失败保留在 `owned-gpu-site-3/`。

## 原控制器退出时序的定位

修复后的宿主协调器 `15db8f6` 继续同一临时站点，使用上面的 `ccf07ef` 冻结镜像。无设备预检通过，GPU worker 从 01:44:05 UTC 开始，完成全部 21 个前置阶段；01:55:57 UTC 再次在首个控制器故障检查退出。原任务独立退出码为 70，新诊断指向 `container_execution.py:115` → `container_monitor.py:286` → `container_processes.py:227,204`：完整 NVIDIA 查询之后，普通归属检查仍要求原任务活跃，因而拒绝已按要求退出的对象。

原日志、退出事件和源码身份保存在 `owned-gpu-site-4/controller-exit-failure.json` 及 `worker-failure.log`。本次自动回退已完成，独立 `automatic-recovery-audit.json` 核对原 CPU 数据、旧代、全部 GPU 容器停止、配置退出、GPU 0 空闲和原服务不变。未运行正式 API 提交。

[退出观察修复](rfcs/029-controller-termination-observation.md)只在固定故障消息发送后使用已绑定的原维护对象；非空清单仍要求活跃归属，确认退出后重新取得完整空清单。254 项相关组件检查通过，后续真实容器与新冻结镜像结果见下节。该结果不能当作新版本 GPU 启动通过。

## 退出观察版本的实测与资源标志问题

修复提交 `b8168d94a1bf1e0a421ec418bb62841ca9067710` 的 351 项启动/控制端回归、补充后的 44 项进程检查通过。真实无设备故障五项、进程归属十八项、原控制端十项全部通过，原任务、标签和临时目录清理通过。证据在 `test-results/termination-observation-2026-11-15/`；这些检查不使用 GPU。

同源 CPU 包 `2026-11-15-cpu-upgrade-target-8` 的清单摘要为 `6fae12f084cd358ea715ed1554ca02670cddf00e440bd4fa89f8e75c436b0e9e`，GPU 包 `2026-11-15-termination-four-image-1` 为 `2b526252efb24a6dee2da6b55d1674578dd6b7a0a41d8924ef9d56117f3a8a9f`。1002 个归档文件与 Git 逐字节一致，source ID 为 `46dc3ae11a61251c1a0f48b83dc6af8f4ef9dd404d393551204e8acb7f1bc77c`。完整核验/导入、十项无设备 worker 检查及 CPU 应用通过，后者用时 431.575 秒，含真实评测、浏览器、准入/排空恢复和清理；结果见 `test-results/termination-releases-2026-11-15/`。

该版本在原站点的 `owned-gpu-site-5/` 通过初始恢复，随后资源检查被进程状态比较拒绝。事件记录显示先有 OOM，约两秒后原控制端发出 SIGKILL，再以 137 退出；没有保存两次完整 inspect 差异。模型只改变活跃原任务的 OOM 标志即可复现同一报错，原始失败保存在 `oom-marker-reproduction.log`。单向标志变化修复及拒绝边界共 391 项组件检查通过。首次附加诊断误用容器内解析器处理宿主 cgroup，报告 `px-container-kernel-364694870edd/` 保留，资源已清理。

修正诊断入口后，`test-results/px-container-kernel-2b9039cb46f0/result.json` 的三组真实无设备检查通过：九项资源检查及十七个 guest 清理、恢复失败时扣留结果，以及新正确调用。实际观察记录确认同一活跃容器、启动时间、PID 1、出生时间、命名空间 PID 和 cgroup 摘要未变，而 OOM 标志从 false 变为 true；原标志没有被改写。容器、临时目录和镜像标签清理通过。这证明该标志可在原进程存活时变化，不是 CUDA 或新版本 GPU 启动通过。

本轮再次自动回退到原 CPU 版本；`owned-gpu-site-5/automatic-recovery-audit.json` 确认数据保留、GPU 空闲、全部原 worker 停止、配置退出和原服务不变。本轮尚未到达控制器故障检查，不能据此认定退出观察修复已通过 GPU；正式 API 仍未验证。

## 正式 worker 完整启动通过后的数据库冲突

`9cc50bc63212552b449c1284255fab44ff6bf192` 的同源包包含 1002 个与 Git 完全一致的文件，source ID 为 `f0de9397ab9be75f9fb3a2e8183c942c7c887e06dfdb30e31e4e4b4509c1d4d6`。CPU 包 `2026-11-15-cpu-upgrade-target-9` 清单摘要为 `ff1c655ae4959068d04d694cb5a2b1f144ca95883ac414e39e1568a6547b5409`，GPU 包 `2026-11-15-oom-observation-four-image-1` 为 `c06cee7dc55393e954f6d5ec2007c188d41c1b930141847704a8054df25ac2be`。构建、核验、导入及无设备 worker 检查通过，CPU 应用完整验收 434.955 秒、清理通过；见 `test-results/oom-observation-releases-2026-11-15/`。

原站点 `owned-gpu-site-6/` 的正式 worker 于 03:06:57 UTC 启动，03:25:50 完成全部 24 个阶段，03:25:51 发布就绪。资源检查、控制器退出、断连和独立硬期限及各自新恢复均通过；这是该固定版本的真实完整启动证据。

随后候选队列检查期间，03:26:40 的心跳 UPDATE 因 PostgreSQL 锁等待超时退出，就绪随后失效。原轮次和测试提交保留，普通 API 尚未验证。`database-error-observation.json` 只记录固定错误类别。原锁升级冲突已在自有 PostgreSQL 重现，修复后的 36 项及旧清理专项三项实际检查通过；112 项相关组件通过，普通环境跳过的四项 PostgreSQL 检查已在实际数据库运行。规则见 [RFC-030](rfcs/030-gpu-database-lock-order.md)。原站点的恢复仍在进行，完整启动不能外推为队列或发布通过。

## 原轮恢复与正式 API 的首轮结果

原 `9cc50bc` worker 的第二次完整启动于 03:53:53 UTC 完成，随后沿原容器、原启动时间与原候选轮次恢复。`4380a19` 调用者使用已核对旧源码的 200 ms 清理等待，没有重新入队或刷新期限。原轮清理和 CPU 回退通过；`owned-gpu-recovery-6-1/final-audit.json` 确认原数据、目录、序列、设备空闲、停止对象和原服务身份。

`4380a19eb6eb5ec187575514dc1a527bedce9687` 的同源包含 1007 个与 Git 一致的文件，source ID 为 `6344ea103f87788d20485219d168ff59999d235cf1e52cb61014c69039867490`。GPU 包 `2026-11-15-lock-order-four-image-1` 清单摘要为 `55919bad9f5b9804e22b1e13f5a0018d6e564b9ee53ed8cea546edc1ad941f9b`，CPU 包 `2026-11-15-cpu-upgrade-target-10` 为 `93162b5775b757492a704a3f157b1680d7af9a6de7dff4127f93748b861e67d9`。完整核验/导入、十项无设备 worker 检查和 CPU 应用验收通过；CPU 应用用时 436.157 秒，结果在 `test-results/lock-order-releases-2026-11-15/`。

原站点新代 `b3e793bde609` 于 04:12:36 UTC 启动正式 worker，04:30:40 完成全部 24 阶段，随后候选正确/错误提交与清理通过，CPU→GPU 发布成功。仅在此合成站点发布完整 CUDA 候选包，经普通 HTTPS 建立两个测试账号和令牌，保持默认每日额度。向量加法的 Run、正确 Submit、三个错解 Submit 和恢复正确 Submit 共六项全部通过，同一完整环境下生成实际控制器计时成绩。

第七项矩阵参考 Run 返回 `time_limit`，没有编译初始化完成字段；原提交 `30ca2fe5-0f0f-4a62-a68b-704abb280a5f` 和请求键、响应、任务容器记录均保留。原时序证明准备阶段消耗约 27 秒，而 Run 的有效执行截止点为预约开始后 35 秒，见 [RFC-031](rfcs/031-gpu-preparation-checkpoints.md)。普通 API 整套报告仍为失败，矩阵其余五项、隐私与浏览器分享尚未验证，不能按向量结果判定全部题目通过。

之后沿原协调器正常发布到 CPU 代 `d401764f045b`。发布前后的三账号五张用户表及完整目录摘要一致；扩展检查覆盖 55 张非运行状态表、33 个序列和全部七个 GPU 结果。发布后独立 CPU 验收只更新自己的草稿并新增一个提交，其他表摘要一致。原 GPU worker 正常退出码为 0、配置退出、journal 无活动任务、GPU 0 空闲，三个原用户服务身份及启动时间不变。证据在 `owned-gpu-site-7/final-audit.json`、`owned-gpu-preservation/` 和 `owned-gpu-api-1/`，均位于本页前述私有证据根目录。

## 准备检查点修复的非 GPU 验证

[RFC-031](rfcs/031-gpu-preparation-checkpoints.md) / [ADR-085](adr/085-gpu-preparation-checkpoints.md) 将开始回调后的强制核对接入原通道心跳时钟，移除紧邻重复检查，保持 90/180 秒预约、各层恢复余量及原绝对期限。268 项相关组件与本地管道检查通过；真实 CPU 容器的通信九项、调用链二十七项及故障五项全部通过，容器、临时存储和镜像标签清理通过。记录在 `test-results/checkpoint-observation-2026-11-15/verified-reports.json`。该结果不证明矩阵 Run 已修复；后续须使用新冻结四镜像重新完成 GPU 启动与普通 API。

## 新冻结版本的两题 API 与实际成绩分享

修复提交 `50d90b595a3b0577e47ac4aa4b90d42dff97b470` 的 GPU 包为 `2026-11-15-checkpoint-four-image-1`，清单摘要 `034d045a8fd7a57f954c39e5ce1e65e933d4e4f6438e3de292ba5879edafe548`；配套 CPU 包 `2026-11-15-cpu-upgrade-target-11` 的摘要为 `85c234861970c7ef3844e93ccfa89df0c7ab236f90ee33b0f9fcac8ed282d3ea`。1009 个归档文件与 Git 一致，source ID 为 `ebfebc66291f8447a512bc05396db418c80c26c93f499d1818ccaae1ac24a6ed`。完整制品核验、导入、十项无设备 worker 和 CPU 应用检查通过，CPU 应用用时 437.072 秒并完成清理；记录在 `test-results/checkpoint-releases-2026-11-15/`。

原站点新代 `d549386187fc` 的 GPU worker 于 05:26:00 UTC 启动，05:40:26 完成全部 24 阶段；随后普通队列中的正确/错误提交及清理通过，CPU→GPU 发布成功。该次复制前后的 55 张非运行状态表摘要完全一致，33 个序列不回退，原三个测试账号及七个 GPU 结果保留。调用者、原容器和完整启动记录在 `owned-gpu-site-8/`。

新建两个合成账号，先验证曾超时的矩阵题，再验证向量题。每题 Run、正确 Submit、三个错解和恢复正确 Submit 全部通过，再各增加一次另一账号的正确 Submit，共十四项；其中八项 accepted、六项 wrong_answer。全部使用同一个完整环境指纹 `4c054fac781934568aef48210d475c3ee6287e12c147fa18489edc696fc31573`。每账号预约 1170 秒，保持默认 1800 秒额度。匿名读取返回 401，另一账号读取返回 404；所有原终态响应均在判定前保存，证据在 `owned-gpu-api-2/`。

矩阵原 Run `43d0a381-1ec2-4077-bdc9-95783584c75e` 的公开样例为 2/2，正式 Submit 为 6/6。Run 的准备到执行标记用时 17.624942 秒，包含编译、运行、新正确恢复和清理的总历时为 32.897992 秒；90 秒预约与全部恢复余量不变。精确原任务时间戳保存在 `matrix-original-task-timings.json`，旧超时任务仍保留。

实际浏览器使用上述服务端成绩，在中英两种语言和 320/390/768/1440 宽度下完成八组检查：未确认时不公开、键盘确认、私有结果不进入榜单、错误 Submit/Run 拒绝分享、跨账号拒绝、同版本同环境的双账号按 0.001 ms 显示值排序，以及分别撤回后的即时排名与访问变化。标题与正文起点、页面溢出和截图也已检查；公开页面不包含设备身份、源码或原始采样。最终两个主页关闭、所有本次公开结果撤回。记录与截图在 `owned-gpu-browser-1/`，使用 Chromium 156.0.8078.4。

以上证明该冻结版本的实际队列、题目、计时与分享；后续切换和中断结果分别记录，不能互相替代。

## 同一冻结包的 GPU 代际切换与 CPU 退出

保持 `50d90b5` 的四镜像不变，从 GPU 代 `d549386187fc` 实际切换到新的 GPU 代 `b3862c62f88d`。新 worker 于 05:56:35 UTC 启动，06:11:24 完成全部 24 阶段，随后新的候选轮次正确/错误提交与清理通过。复制前后 55 张非运行状态表、23 项提交和六条分享记录的摘要一致，33 个序列不回退；其中包括原七项 GPU 结果、新十四项 GPU 结果及两个 CPU 结果。此项验证同一冻结包的代际与数据库切换，不宣称不同 GPU 程序版本之间的升级。

之后正常切换到配套 CPU 包的新代 `646d4b55817c`。相同的完整表、目录、账号、会话、成绩、令牌和分享记录比对通过；切回后又经 HTTPS 完成一个新的正确 CPU 正式提交。两个原 GPU worker 均正常退出码为 0，GPU 配置退出，journal 无活动任务，数据库无物理预约，GPU 0 空闲。两个测试主页关闭，六条分享均保持撤回；原 Web、CPU worker 和 tutor 的 ID、镜像、启动时间及健康状态不变。最终证据为 `owned-gpu-site-8/final-audit.json` 和三组 `before-generation-*` / `after-generation-*` 快照。

至此，该固定版本的 CPU→GPU→GPU→CPU 正常流程通过。对应源提交的[托管应用检查](https://github.com/laf070810/PraxiMesh/actions/runs/43680238591)和[完整 WASI 工作流](https://github.com/laf070810/PraxiMesh/actions/runs/43680238579)也均成功。原站点继续用于发布中断验收，不能据正常流程结果开放尚未验证的公共 GPU 恢复入口。

## 两次实际协调器中断与曝光后的新写入

同一 `50d90b5` 包在原临时站点建立 GPU 代 `6bc7c2cece94`，完整启动和候选队列通过。向 CPU 发布时，在已持久化 `exposing`、尚未保存曝光决定的位置暂停原调用者；核对原 PID、出生时间、UID、命令及工作目录后，经 pidfd 向该调用者发送 SIGKILL。普通恢复重新启用原 GPU 数据库和原容器，并完成新的 24 阶段启动，没有借用上次启动证据。

随后在恢复已持久化原 GPU 曝光决定、路由已开放但外部健康确认尚未返回的位置，再次暂停调用者。通过普通 HTTPS 登录并完成新的正确 CPU Submit `1b94bd75-3a23-41db-a6af-ab3a48fbb70d`，保存完整表快照，再以相同身份核对方式终止调用者。普通恢复保留曝光后的新写入，全部表摘要一致；原 GPU worker 的完整 ID 与 `2026-11-15T06:44:28.958592231Z` 启动时间均未改变。

这两项实际中断通过，证据在 `owned-gpu-faults-9/` 的两个 checkpoint、原调用退出码、前后数据库快照和 `worker-startup-history.json`。它们证明曝光前回到原库，以及原库恢复曝光后的响应丢失续跑；不等同于新 GPU 候选曝光后的全部故障场景。整套报告仍未标记完成，因为后续 worker 故障验收尚未通过。

## 外部 GPU 占用与容器查询漏报

worker 崩溃验收的首个普通 API 任务因观察端轮询触发限流而停止验收，第二个任务在核对设备进程归属时发现其他账号的 Slurm 作业；两次均未实际终止 worker。原请求最终分别为 `time_limit`，预约正常结算，物理预约为零，原响应和失败记录分别保留在 `owned-gpu-worker-crash-9/` 和 `owned-gpu-worker-crash-9-2/`。不能将这两次尝试计为崩溃恢复通过。

宿主当时列出 17 个外部进程，原可信容器内的固定完整 SMI 却返回空列表。确认原任务结束、journal 空闲和无物理预约后，于 07:31:20 UTC 正常停止原 GPU worker，退出码为 0；没有终止外部进程或重置 GPU。随后同镜像的只读 NVML/SMI 对照确认私有 PID 命名空间漏报，单独绑定宿主 `/proc` 仍漏报，共享宿主 PID 后两来源与宿主集合一致。详细边界见 [RFC-032](rfcs/032-gpu-observer-pid-namespace.md)。

此前两题 API、浏览器和正常代际切换完成于外部作业出现之前，仍保留其原成功范围；这些结果不能证明外部占用检测正确。当前停止 CUDA 验收，继续修复管理观察与启动契约。

随后正常发布到配套 CPU 包的代 `1c25ce130649`，没有重启 GPU worker，GPU 启动尝试数为零。55 张非运行状态表摘要一致、全部序列不回退、物理预约为零、配置正常退出；再经 HTTPS 完成新的正确 CPU 提交。外部 GPU 进程只观察，不操作。325 秒的原调用及前后数据证据在 `owned-gpu-retirement-9/`；临时站点、旧数据库及所有历史继续保留。

## 进程观察修复的实际验证

[ADR-086](adr/086-gpu-observer-pid-namespace.md) 已接入完整 PID 视野检查、版本化创建记录、冻结归档的协议能力核对，以及旧记录只停止/退役的边界。715 项相关组件、25 项真实管理/CPU 容器、十项实际 worker/Compose/PostgreSQL 和十项本地控制器/客户端检查通过。新代码也按原完整 ID 核验了本页两个已停止的 schema 2 对象，部署文件未变。

管理查询重放时 GPU 0 已空闲，宿主和可信容器的完整列表均为空；任务保持私有 PID。该结果与前面实际外部占用时的只读对照分别保存，不把本次空闲结果当作外部占用正例。全部自有测试资源已清理，原用户服务未变。当前仍需新冻结四镜像的完整 CUDA 启动、正式任务和故障恢复；上述检查不签发就绪凭证。

## 新 PID 契约的首轮 CUDA 启动与回退

提交 `f27d085e1949c84799b7fabe0b1c9f6ee3295da1` 的 1011 个归档文件与 Git 一致，source ID 为 `fbc0ffda9e61b7924f54975cff8c977aa1dcb3c1b5f6cd717b700706db8054a5`。四镜像包 `2026-11-15-host-pid-four-image-1` 的清单摘要为 `8a0249169f97195fb48ab969e630155bcb903ce2006699b776194fd5a8cd30a2`，配套 CPU 包 `2026-11-15-cpu-upgrade-target-12` 为 `f52237506c3e435716360fee12969b8c3cdffa40b64ca10dbc7abfdef99bb553`。标准核验、导入、十项无设备 worker 和完整 CPU 应用检查通过；CPU 应用用时 434.071 秒并完成清理，记录为 `test-results/host-pid-releases-2026-11-15/`。

确认 GPU 0 空闲后，原站点新代 `bf26ff6714e4` 的 schema 3 worker 于 09:14:14 UTC 启动，09:14:42 因 `ProcessDisappeared` 退出，未完成首个启动阶段。错误来自 NVIDIA 列表之后的宿主 PID 归属读取；原维护任务 `f6eab90eaf116c999f17bf0b656ba71dc89ace04ac1756ff0cac931b7fad9b23` 已按原记录清理，没有签发就绪或开放提交。

协调器自动退回原 CPU 代 `1c25ce130649`，GPU 配置正常退出。55 张业务表、33 个序列、无物理预约、设备空闲及三个原用户服务不变均已核验；证据为 `owned-gpu-site-10/rollback-audit.json` 和 `after-rollback-data.json`，原启动失败报告保持失败。

同一实现随后沿原本地状态和固定参考解复现，宿主只读记录证实短暂 NVIDIA PID 属于原任务 cgroup，普通恢复成功。下一步按 [RFC-033](rfcs/033-gpu-pid-observation-order.md) 调整查询与身份读取间的重复检查，并用实际 CUDA 重放验证；尚不能判定该竞态已解决。

## 原状态重放与短暂进程的归属

后续各轮均沿原本地 GPU 状态、同一固定公开参考解及普通恢复运行，不用新状态跳过历史。`07fb091` 的查询顺序调整仍失败；`47bdfe2` 的受限诊断确认 NVML 与稍后单次 XML 存在时间差，原状态恢复成功。`598ff4e` 改为两次完整 XML 包围 NVML，随后失败于 PID 651065 的已退出状态；完整身份当时仍可读取，来源窗口结果不能算完整启动通过。

`6df9c6b` 将完整已退出身份与存活核对分开，328 项相关检查通过；真实重放运行 132.877 秒后仍因 PID 964938 的目录被回收而失败。旁路样本中的原出生时间、NSpid 和 cgroup 仅用于定位，不能代替执行时的归属证据。普通恢复成功，原状态 recovered、无活动 journal、GPU 0 空闲。各轮原日志分别在 `test-results/` 下的 `pid-order-local-diagnostic-2026-11-15/`、`pid-failure-local-diagnostic-2026-11-15/`、`inventory-window-local-diagnostic-2026-11-15/` 和 `exited-member-local-diagnostic-2026-11-15/`。

下一轮按 [ADR-090](adr/090-sampled-gpu-process-handles.md) 在每个完整 XML 样本返回时绑定身份和原 pidfd，仅用同次查询的句柄确认后续退出。345 项相关检查含实际 CPU 子进程与句柄关闭通过；匹配镜像的 CUDA 重放尚待完成，服务器提交仍未开放。

该轮 `cbe8734` 的镜像内无设备 CPU 检查通过，真实 CUDA 随后失败于目标 pidfd 的多层 NSpid 核对，普通恢复完成。实际无设备容器的前后对照确认，应比较目标完整命名空间链；修复后的原句柄与自然退出检查、351 项相关回归通过。新匹配镜像的完整启动仍需重放，不能以 CPU 对照结果宣布 GPU 已恢复。

`62eb989` 的匹配控制器与原已核验任务镜像运行 1126.844 秒，未再出现进程归属故障，但最后的 service-deadline 检查在 EOF 后核对超时。原容器独立退出码为 124；Docker 事件与事后时钟对齐显示退出发生在观察截止之前，不能据此补签原检查成功。普通恢复完成，原状态 recovered、无活动 journal、GPU 0 空闲；记录在 `test-results/namespace-handles-local-diagnostic-2026-11-15/observation-1/`，退出事件为 `deadline-events.json`。原 Web、CPU worker、tutor 的 ID、镜像、启动时间与健康状态另行复核不变。

原代码上带检查耗时的模型复现同一异常，按 [ADR-091](adr/091-controller-fault-observation-checkpoints.md) 共用传输层检查时钟，EOF 后仍读取新状态，原期限不变。371 项相关组件检查通过，实际 CPU 故障矩阵和新双镜像的 CUDA 重放继续进行。该修改也属于任务镜像源码范围，不能只替换控制器；为 `62eb989` 准备的后续发布和服务器验收脚本尚未执行。

标准 CPU/runsc 矩阵随后五组全部通过，含主动退出、断连、独立期限、准备失败和恢复失败；原进程、临时目录及镜像标签均已清理，源码保持不变。报告在 `test-results/px-container-lifecycle-aa1cb3a4edea/result.json`。该结果不签发 GPU 就绪，后续真实重放仍须使用新匹配双镜像。

## 资源检查中的整任务 OOM

`7b2b7a7` 通过标准构建生成匹配双镜像，镜像内 CPU 句柄检查也通过；原状态 GPU 重放在 126.222 秒因资源检查任务整体 OOM 而断开，尚未再次执行最后的独立期限场景。原任务先有两次 OOM，随后退出码 137，距离硬期限仍有约一分多钟。普通恢复完成、GPU 0 空闲，原日志与事件保留在 `test-results/fault-clock-local-diagnostic-2026-11-15/observation-1/`。此失败不能记为内存隔离通过，也不能用它推断退出期限修复失败。

不访问 GPU 的资源矩阵与内存采样显示，原 guest 子进程被 OOM 终止后，共享内存仍会短暂留在原 cgroup。按 [ADR-092](adr/092-nested-sandbox-oom-priority.md) 为私有 sandbox/gofer 固定更高淘汰优先级；351 项相关组件和三组实际 CPU 资源检查通过，分数、原监督进程、内存释放、清理及正确恢复均有实际观察。原 512 MiB 配额、期限和整个任务 OOM 必须失败的条件不变，GPU 路径仍需新匹配镜像重放。两轮 CPU 原始数据分别在 `test-results/resource-memory-diagnostic-2026-11-15/` 与 `test-results/resource-oom-priority-diagnostic-2026-11-15/`。

`2210e04` 的新双镜像实际重放已越过资源检查及后续正确恢复：一次 OOM 后原监督进程继续存活，内存下降，原总限额不变。随后运行到最后的 service-deadline 场景，在查询后对非空列表执行严格存活核对时，原 PID 1 已消失，完整启动仍失败。原容器退出码 124，普通恢复完成、GPU 0 空闲；原 1102.295 秒调用、内存采样和退出事件在 `test-results/oom-priority-local-diagnostic-2026-11-15/observation-1/`。此前准备的该提交发布与服务器脚本尚未执行。

该边界按 [ADR-093](adr/093-terminating-task-sampled-identities.md) 改用同次查询绑定的原任务/成员句柄，并在原期限内确认同一容器停止后重查完整空清单。391 项相关检查和五组实际 CPU/Docker 故障矩阵通过，其中独立期限采样确实跨越原进程退出。CPU 清单是明确夹具，尚不能证明新 NVIDIA 退出观察成功；后续仍须新匹配镜像重放完整 GPU 启动。

## 当前查询身份修复后的完整本地启动

提交 `6e40ba770db11a65bc51b2a9cbf91cf59decf236` 的标准双镜像在原物理状态通过完整重放：24 个阶段逐项与固定检查目录一致，向量加法公开样例 2/2 accepted，正常命令退出码 0，用时 1221.741 秒。资源 OOM、控制器退出、断连及最后的独立期限均完成各自清理和新正确恢复；没有改变配额或原期限。

独立审计核对本轮 28 个任务和原控制容器均已移除，原操作与控制记录为 completed，无活动 journal，GPU 0 完整清单为空。原 Web、CPU worker、tutor 的完整身份、启动时间、重启次数和健康状态均与基线一致。结果仍为 `formal_result: false`、`ready: false`；这轮未产生服务器正式成绩。

完整身份和原始证据见[本地启动记录](acceptance-gpu0-startup.md#current-process-observation)。对应四镜像发布包、正式 worker、API 和 worker 丢失恢复仍需分别验收；此前所有失败记录继续保留。

同一源码的[冻结四镜像与配套 CPU 包](acceptance-current-releases.md#termination-window-release)随后通过完整归档核验、十项无设备 worker 检查及 435.777 秒 CPU 应用验收。原临时站点从 CPU 代 `1c25ce130649` 升级为 GPU 代 `dfb8aece2e84`；原 worker `37ba90d14ad7904bf853e54ae1b224f6f939ddb55fc20173ae799969ee951317` 于 13:59:11 UTC 启动，14:13:33 完成全部 24 阶段并发布就绪。正常队列的候选正确/错误提交与清理通过，CPU→GPU 发布成功。

发布前后 55 张非运行状态表摘要完全一致，33 个序列不回退，原成绩、账号、目录与历史保留。原始状态和逐阶段时间线在 `test-results/uvm-releases-2026-11-15/owned-gpu-site-14/`，其中 `initial-startup-audit.json` 绑定原镜像、容器和启动时间。该结果证明这次发布与候选队列；普通 API、浏览器及实际 worker 丢失继续分别验收。

## 待完成的真实范围

重查原卡独占后，使用新冻结版本验证正式任务中的 worker 丢失、原预约清理与正确性恢复，以及升级和曝光恢复。已有结果只证明上述固定版本与实际范围；全部成功前，仍按[需求追踪](requirements.md)保留网站 GPU 发布门槛。
