# 阶段 A 验收记录

更新：2026-10-23（Asia/Shanghai）。基线：[PRD v0.2](../PRD.md)。状态：**阶段 A 的可自部署技术交付已完成；公开实例与托管 CI 服务端验收尚未完成。** 默认执行关闭，未公开发布。

## 按产品场景核对

| 场景 / P0 要求 | 当前证据 | 验证边界与后续事项 |
| --- | --- | --- |
| AC-01（R-01、03）游客与知识导航 | 4 个领域、3 个主题、8 个稳定概念、4 篇教程；跨领域共享概念；双向知识/题目链接；桌面与移动浏览器检查 | 更大内容规模属于后续扩展 |
| AC-02（R-04）完整学习路径 | 1 条自由跳转路径，4 篇教程、8 道练习；真实 Run 不计完成，正式 Accepted 推进至 1/8 | 扩充路径属于 P1；公开内容发布仍需维护者确认 |
| AC-03（R-02、07）直接练习 | 搜索/多维筛选；编辑、私有草稿保存恢复；HTTPS 浏览器实际错误提交→修改→通过；无需先读教程；六账号真实并发提交 | 目标生产节点的持续负载与公网延迟目标 |
| AC-04（R-06、07）真实在线判题 | CPython/WASI 实际执行；21 项资源/能力探针；宿主 canary 拒绝、父 worker 被杀后驱动退出；真实参考/错误解；浏览器运行到结果闭环 | 目标节点需重跑隔离探针与发布检查；gVisor 后端未实测，不列入已验证交付范围 |
| AC-05（R-05）账户/隐私/权限 | 注册/登录/POST 退出、恢复码轮换和旧会话失效；私有草稿/记录隔离；XSS/CSRF/限流；导出/删除；真实 TLS/代理、Secure cookie 和浏览器删除 | 公有证书/续期、生产备份删除保留政策 |
| AC-06（R-08）内容质量与迭代 | 严格 schema/引用/DAG；原子发布、不可变修订、历史保留和撤回；8 道题（3 道 v3、5 道 v2）、51 个实际通过的参考用例；16 个针对性错误实现与 8 个恒空值实现均被拒绝 | 本轮已交叉核对初始内容契约；正式授权与公开制品许可复核仍由维护者确认 |
| AC-07（R-09、10）部署与交付 | 固定 Web/worker 制品与逐层校验；持久排空及任务策略绑定；真实 HTTPS 多代发布、全表备份恢复核对、Caddy 重启、开放前中断恢复旧站、开放后保留新写入/删除；CI/CD 定义与服务示例 | 托管 CI/CD、目标主机服务监管与持续容量、公有 TLS、异机灾难恢复及发布复核 |
| AC-08 新题型 | evaluator 注册表、统一任务/结果/版本边界及两种隔离策略 | 后续验证第二种题型；增加隔离后端不等于新增题型 |

## 当前版本的实际检查

- 当前普通 SQLite 回归：**310 passed、18 skipped，15.1 秒**；跳过的是 5 项真实沙箱与 13 项 PostgreSQL 专用检查。
- 当前临时 PostgreSQL 完整回归：**327 passed、1 skipped，133.0 秒**，包含真实 WASI、发布档案、暂停/恢复、策略隔离、数据库并发/超时、候选验证重试清理、部署控制流程及配置边界，并补充等价用户名限流、历史草稿权限/版本/分页和私有诊断留存；跳过项只验证 SQLite busy timeout。控制流程中的替身测试与下文真实容器证据分别记录，不能用替身代替执行验证。
- 真实沙箱套件含 21 项版本、正确/错误分类、fuel、阻塞调用墙钟、内存、输出、空环境、只读库、宿主路径/遍历拒绝、无文件写入/网络/进程/原生执行/线程、FD 上限、伪造退出码和跨实例状态检查。另以实际存在的宿主 canary 验证拒绝读取，并用内核进程句柄确认监督进程被杀后驱动退出。
- 8 个参考实现通过全部 51 个用例，包含声明上限；16 个典型错误实现、8 个返回 None 的实现及伪造 accepted/评分返回值均未通过。涉及输入保留或新对象的题目将这些做法明确为建议，所有题都只按返回值判断；二分题不声称认证渐近复杂度。
- CPython 3.14.9、WASI SDK 24、Wasmtime 50.0.1；再次完成固定源码构建。当前 bundle format 2 显式设置 `x86_64-unknown-linux-gnu`、关闭 component model、保留固定运行库声明。manifest SHA-256 为 `35a76bda43a52c1db3ba0e4627922264d26e4486dc17722d49c7b374fbba216b`，位于 `var/wasi-python-3.14.9-v3/`。旧 v2 目录是 format 1 历史制品，当前策略会拒绝它；目标节点仍须实际验证。
- Playwright Chromium、`BROWSER_JUDGE=1`：注册→草稿保存恢复→Run 通过但进度为零→错误 Submit→修正 Accepted→路径 1/8 和三条历史→删除账号；桌面与 390 px 页面，无 pageerror、检查页面无横向溢出。Web、worker、数据库由脚本创建并清理，监听范围仅回环。
- 应用镜像的独立烟雾基线通过：UID 65532、只读根、无网络、迁移/导入/Gunicorn/静态资源 HTTP。Web 镜像含策略与锁文件，不包含 WASI 制品或 Docker socket；该烟雾检查保持执行关闭。最新两镜像的真实部署检查见下文。
- `content_check`、Ruff、迁移漂移、Django `check`、锁文件/生产 requirements 一致性通过。`pip-audit` 包含新增 judge 依赖，查询时未发现已知漏洞；不表示基础镜像系统包或未来漏洞已获证明。

本地证据在忽略的 `test-results/`，当前为 `postgres-phase-a-final.xml`、`application-phase-a-final.xml`，另有浏览器截图、受限日志和构建日志；旧文件保留为历史基线。重放见 [WASI 指南](wasi.md)；WASI 工作流在临时 PostgreSQL 执行完整真实套件，随后执行 HTTPS、镜像导入与部署恢复验收，尚未在托管 CI 服务端运行。

## 真实 HTTPS 部署与容量（2026-10-23）

`scripts/production_acceptance.py` 已完整通过：Caddy 2.11.7 → Gunicorn 2 进程 × 2 线程（生产设置）→ 独立 PostgreSQL 17，外加 1 个真实 WASI worker。所有端口仅回环；Python 3.11.15、x86_64，共享主机允许 192 个 CPU，Web 没有专门 CPU 配额，PostgreSQL 限 512 MiB。该配置不是两核生产主机的容量证明。

这次 HTTPS/容量记录先于暂停协议的加入；随后未修改 TLS/代理实现。新队列协议的验证独立记录在当前 PostgreSQL 与容器验收中，不把旧测量冒称为新发布包重新测得的容量。

- TLS 1.3、私有 CA 链/主机名验证、错误 CA 拒绝、未知 SNI 握手拒绝、HTTP 重定向、HSTS/CSP、100 KB 请求体上限、非法 Host 和生产 404 检查通过。浏览器仅对临时 leaf SPKI 设置证书例外，未安装系统 CA 或全局忽略证书错误。
- 五个新增代理回归场景通过；实际代理覆盖伪造协议/IP，未受信连接不能利用 HTTPS 转发头绕过重定向。65 次伪造不同 X-Real-IP 的账号请求仍进入同一实际来源额度；数量覆盖跨窗口突发上限。
- Web 不能 DDL、修改题目版本或健康记录；worker 不能读 `auth_user`。在这些角色下，真实 HTTPS 浏览器闭环、账号删除和六账号判题全部通过。生产远程数据库 TLS 未包含在这次回环测试。
- 停止本次 worker 并等待真实心跳过期：提交 503、不新增任务，阅读和私有草稿仍可用。临时服务、数据库、证书和凭证均已清理。

| 样本与并发 | 实测结果 |
| --- | --- |
| 空闲判题时 8 客户端、120 次 HTTPS 页面请求 | 0 错误，p95 90.1 ms、p99 99.1 ms |
| 判题期间 8 客户端、120 次 HTTPS 页面请求 | 0 错误，p95 97.2 ms、p99 100.2 ms |
| 6 账号同时提交，每份 6 用例，1 worker | 6/6 Accepted，总耗时 p50 27.6 秒、最大 56.1 秒 |
| 同一提交突发的排队与执行 | 排队最大 47.3 秒，单份执行最大 8.8 秒 |

分位数按 nearest rank；6 个任务的 p95/p99 均为最大值。满足本地回归目标“浏览 p95 < 1 秒、六份突发 < 60 秒”，但小样本不证明持续峰值或公网 SLO。单 worker 的排队是当前主要瓶颈，全局 100 个待处理任务是拒绝入队的硬上限，不是可在满意延迟内完成 100 份任务的承诺。

完整机器可读结果为 `test-results/production-acceptance.json`；重放方法见[部署验收指南](production-acceptance.md)，决策见 [ADR-005](adr/005-production-acceptance.md)。WASI 工作流已接入同一脚本，尚无托管服务端运行记录。

## 发布制品与容器核心闭环

同一源码快照生成 Web 与 WASI worker 两个非 root 镜像，导出包含共享层的约 270.5 MB Docker archive。校验器检查可信 manifest、全部源码摘要、路径/类型/解压边界、实际镜像配置及每个 layer 的 diff ID；已验证缺失或修改 layer 在导入前被拒绝，不依赖本机已有镜像缓存。

实际导入后在新的 PostgreSQL 和独立网络中启动两者：Web 不包含 Wasmtime/bundle，worker 无 Docker socket 或宿主目录挂载，只连内部数据库网络，启用只读根、capability 删除、init、内存/PID 限额。镜像内真实策略摘要与发布 manifest 一致。worker 在当前容器执行 21 项探针后才开始取任务；浏览器的 Run、WA、Accepted、路径/历史和删除账号全部通过。错误解释器摘要会阻止服务启动。

此前独立制品验收通过 `var/releases/2026-10-23-candidate-5/`，可信构建输出的 release manifest SHA-256 为 `95dee546759a91a58455109b172649bf81afa3c5bc903737064a05c60512ac5a`。其中的 Web/worker 包含暂停协议与 `0002` 迁移。后续协调器的实际制品与结果见下一节。构建后补写的验收文档不修改已冻结的源码档案。

同次容器验收实际提交延迟受限的正确函数：零秒排空确实超时且保持关闭，新请求 503，阅读/草稿正常；原任务完成 Accepted 后排空结束但仍暂停；错误 token 被拒绝；原 token 恢复后，第二份新提交也实际 Accepted。自己的测试账号/提交被删除，临时容器、网络与数据库卷清理通过。队列协调见 [ADR-007](adr/007-admission-coordination.md)及[操作指南](deployment-coordination.md)。

证据为 `test-results/release-acceptance.json`、`release-worker.log`、`release-browser.log` 和发布目录的 `release.json`；设计见 [ADR-006](adr/006-release-artifacts.md)，重放见[制品指南](releases.md)。这是打包核心的回环 HTTP 验收，生产 TLS 由独立验收验证。镜像导入本身没有激活现有服务、执行生产迁移或发布 registry。

清理复查发现旧脚本强制删除容器时会留下匿名 PostgreSQL 卷，已按明确的容器事件归属删除 10 个无使用者的合成测试卷，记录在 `owned-volume-cleanup.json`。当前镜像验收改为显式标记并删除数据库卷、验证卷消失；HTTPS 脚本强制清理容器时也要求删除其匿名卷。没有执行全局 prune。

## 真实发布协调与恢复

`scripts/deploy_acceptance.py` 已用 `var/releases/2026-10-23-candidate-7/` 完成五次代际部署尝试，包括一份真正失败的迁移制品。正常发布 manifest SHA-256 为 `3487592ae939f38907a614334e30129213db3ff3b0b38f520810cb91c393301e`，故障制品为 `00a4cc546ea04fb1bf73e01f576a2c490db19c467583fbd8d4606369e86b6b61`。前一份 candidate-6 的四代结果保留作基线。

- 首次安装与同版替换通过真实 HTTPS 浏览器 Run→WA→Accepted、路径/历史和删除。升级冻结旧数据库，备份/恢复后逐表比较 19 张表及 12 个序列，再迁移、最小授权和启动；真实账号、私有草稿及已完成提交保持不变，新账号 ID 不回退。
- 故障制品通过正式构建器从已校验源码生成，仅额外加入说明和原子迁移。迁移真实建表后在 PostgreSQL 内抛出固定异常；实际服务日志确认失败，候选表和迁移记录均不存在。协调器恢复旧代，保留用户记录及备份，随后又完成真实 HTTPS Accepted。未改写 manifest、journal 或判题结果。
- Caddy 被 SIGKILL 后，从相同持久配置恢复；仅清除确认无监听进程且归属/inode 不变的旧 socket。
- 候选开放前强制终止协调器，`recover` 重新验证并恢复旧库/旧站。开放后先通过真实 HTTPS 新增草稿和 Accepted 提交、删除另一账号，再终止协调器；恢复保留已开放候选库、新增数据和删除结果，旧快照中仍存在的账号没有复活。
- 每次启动 worker 均重新运行真实隔离探针；候选检查实际执行正确/错误程序，匹配本 worker 的新心跳、制品与策略。临时容器、网络、数据库卷和本地进程均清理，receipt 的 `cleanup.passed=true`。

完整结果为 `test-results/deployment-candidate-7/deployment-acceptance.json`，重放见[部署协调指南](deployment-coordination.md)。成功替换使用同一应用 schema；失败迁移则确实执行 DDL 后回滚。这不证明任意未来 schema 变更兼容，也未验证公有 CA、目标系统服务、异机恢复或持续容量。恢复边界见 [ADR-008](adr/008-deployment-generations.md)。

此前 worker 重启补充验收制品是 `var/releases/2026-10-23-candidate-8/`，manifest SHA-256 为 `9cd00ac2ff40569aca24813063e5fcabb896b65d8a8d6201b93d530a69125982`。它修复协调器对既有重启计数的误判：等待期间新增重启仍拒绝，但不因等待开始前已有重启而拒绝当前启动探针。额外真实演练只终止所属 worker 控制器，观察自动重启计数从 0 到 1、当前启动尚未就绪，然后立即执行 `recover`；恢复保留同一已开放数据库及用户记录，再次得到真实 HTTPS Accepted，全部临时资源清理通过。Docker 随后的主动 stop/start 将计数重置为 0，记录按实测保存。

该补充证据为 `test-results/deployment-worker-restart/deployment-acceptance.json`。candidate-8 没有重跑五代矩阵；已比较其应用源码、评测身份以及 Web/worker 的全部 rootfs layer，与 candidate-7 一致，差异是控制器、验收、CI 和文档，记录在 `release-runtime-equivalence.json`。完整 CI 流程现在会同时执行五代场景和 worker 自动重启场景，托管执行仍待验收。构建后补写的结果文档不改变冻结制品。

## 最终交叉审查与当前制品

本轮修复四项实际发现：等价 Unicode/空白用户名曾可获得新的账号限流额度；旧草稿链接曾只打开当前版本；三题文字曾要求评测器未检查的输入不变性或新对象身份；失败管理容器删除后曾丢失 stdout/stderr 根因。新增回归覆盖登录/恢复规范化、旧版及下线草稿的本人权限与分页，以及私有诊断的脱敏、字节上界、权限和写失败清理。三题升至 v3，参考解和 51 个用例保持不变，并在当前完整 WASI 回归中再次实际运行。

当前制品为 `var/releases/2026-10-23-candidate-9/`，manifest SHA-256 为 `f762d888e51eafb442c452b48eb0b39bbe3fde834c29fc8296efb6ab4af29628`。同一冻结源码构建了 Web/worker；源码身份为 `eb989d93c70edfe9429288730a9e4e576dfacaa87409db72bb80e42fcbe59f13`。

`deploy_acceptance.py --migration-only` 在两个真实部署尝试中通过首次安装、HTTPS 浏览器 Run→WA→Accepted、私有草稿链接的键盘往返、路径/历史、移动端与删除账号，并用既有固定失败迁移制品触发实际 DDL 后抛错。当前协调器保留了 0600、有界且不含原始配置秘密的根因诊断；迁移表/记录回滚，旧数据库及用户记录保持，再次实际 Accepted。临时容器、网络、数据库卷和本地进程清理通过。诊断原文件只留在临时私有状态内，未复制到测试 artifact。

证据为 `test-results/deployment-phase-a-final/deployment-acceptance.json` 和 `browser.log`。这次专门复验当前变更，没有重跑五代中断/worker 自动重启矩阵，也没有验证成功的 schema 变更；既有矩阵证据仍分别对应 candidate-7/8。当前普通和 PostgreSQL/真实 WASI 全回归分别为 310/327 项通过，结果文件见前文。构建后补写的验收文字不改变冻结制品。

## 已有运维证据及验证范围

此前迭代已实际验证 SQLite 在线备份/0600 文件/integrity_check/独立恢复。当前协调器重复验证 PostgreSQL `pg_dump`→独立库 `pg_restore`，并比较所有表行、列定义和序列状态；生产实例与实际保留政策仍未验证。受限角色和生产 `check --deploy --fail-level WARNING` 也在上述部署重新通过；静态检查自身不验证 DNS/TLS，TLS 的证据来自实际请求。

此前的小题库顺序 HTTP 基线和约 0.7 秒/参考用例的独立执行数字，不能替代上面的端到端提交测量。目标生产节点仍须测持续负载、恶意超时负载、排队、尾延迟和限流。默认每用户 2 个待处理任务、全局 100，排队十分钟到期按平台故障处理。

gVisor 在本机仍因缺少 runsc、rootless Docker CPU quota 不可用而拒绝，未修改共享 daemon，也没有普通 Docker 或宿主 Python 回退。WASI 的实测结论限于基础标准库函数题、Linux x86_64 和已固定制品；不支持额外原生扩展、GPU/ML/Agent 任务。

## 技术交付与公开发布边界

本次按 R-01–10、AC-01–07 核对了代码、初始内容、真实执行证据和维护文档，修复了账号限流的用户名规范化、旧草稿可达性、三题契约措辞以及部署失败诊断留存。可自部署技术交付以当前文档支持的 Linux x86_64 / WASI 基础函数题为范围。八道题采用 PRD §9.1 的质量优先数量调整；扩大题库不构成当前技术交付的缺失项。

公开运营仍需以下外部输入与验收：

1. 提供目标仓库、专用主机和域名，配置受保护的 CI/CD 环境并实际运行；在目标主机重跑安全探针、系统服务、持续/恶意负载容量、公有证书/续期及恢复检查。已提供自动部署流程，本地验收不替代目标环境验收。
2. 维护者按 PRD §13 确认品牌、代码/内容许可、公开实例/注册政策、额度、治理、安全报告渠道和隐私地域/保留周期；正式公开内容及第三方制品授权由维护者复核。

当前没有配置公开实例或 Git remote，未发布服务、推送仓库或授予公开许可证。阶段 B/C 的社区、排行榜、更多语言、GPU/ML/Agent 评测未实现，留在后续阶段。
