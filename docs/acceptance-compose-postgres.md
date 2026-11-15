# PostgreSQL Compose 的独立验收

更新：2026-11-16。固定应用源码 `7b4fb25`、准备工具 `503c7de` 的 PostgreSQL、Web、CPU/WASI worker 和本地 CPU AI 已通过两轮独立 Compose 启动与同版本重建。GPU 组合首次完成全部启动检查、两题十四项 API 和真实 AI 页面；重建时出现外部占用，原 worker 拒绝启动，数据已核对保留。GPU 重建成功及跨版本维护仍待完成；本页历史结果不能替代每次实际就绪。操作集中在[PostgreSQL Compose](deployment/compose-postgres.md)。

## 组件与制品

独立应用入口、初始化阶段、数据库最小权限和原宿主安装标记已实现。27 项真实 PostgreSQL 检查覆盖迁移、四步初始化响应丢失、两处实际子进程 SIGKILL、授权确认丢失时保留连接/新写入，以及原数据库或 PostgreSQL 元数据丢失后的拒绝重建。46 项文件与配置检查通过，原文件锁、目录 inode、角色凭据和 SQLite 入口保持原范围。

源提交 `7b4fb252e4605614c970f918fb114e5f265f0db6` 的 1045 个冻结文件与 Git 逐字节一致，source ID 为 `e31f84817273afea845d608527f00ba01389bd7cc4bf95a4b8eb6db3c1e301ec`。

| 制品 | 不可变目录 | release.json SHA-256 |
| --- | --- | --- |
| CPU | `var/releases/2026-11-16-cpu-upgrade-target-20/` | `296973de28af8d015d473d0ffd2cfaa6da05a53dff42ea39b173faea0733018b` |
| 四镜像 | `var/releases/2026-11-16-compose-preparation-four-image-1/` | `223c807012581d8a7404ae9c8371d501ab58eb049b0990c0e80fffe7845c0a48` |

两包完成构建和导入；Web/CPU 镜像、源码与 WASI 身份相同。十项无设备 worker 检查及 435.574 秒完整 CPU 应用验收通过，包含真实隔离、浏览器、排空和恢复；临时容器与数据库卷已清理。源码与命令证据在 `test-results/compose-preparation-releases-2026-11-16/`。此前 `7e1aff0` 的冻结包及 434.090 秒应用验收保留在 `test-results/compose-bootstrap-releases-2026-11-16/`，不覆盖新增宿主标记。

## 实际 PostgreSQL、CPU 与 AI 组合

在新的私有安装目录执行公开准备命令，两次相同准备均成功且未启动服务。随后用其返回的真实 Compose 命令启动 PostgreSQL、一次性 bootstrap、Web、CPU worker 和本地 CPU tutor。首次启动至健康用时 170.724 秒；Web/worker 保持最小数据库角色，只有初始化服务持有所有者凭据，应用及初始化均没有设备或 Docker socket。

正常注册账号完成七种题型的正式正确 Submit、一个错解和一个公开 Run，结果为八项 accepted、一项 wrong_answer。实际浏览器另建合成账号，完成私有计划、显式输入预览、一次真实本地模型请求、跨账号拒绝、导出和删除；这些浏览器账号均已删除。截图另行查看，标题、说明、输入与模型回复的起点一致。模型流程验证传输和界面，不替代回答质量审查。

保存私有草稿、学习路径和原会话，并在此合成目录下撤回一道题目后，执行保留卷的 `down` 和原参数 `up`。重建至健康用时 163.892 秒；前后 55 张业务表摘要一致，33 个序列不回退，原会话和草稿可继续使用，下架题目没有被重新发布。重建后又有一项正常正确 Submit 通过。

最后停止原 CPU worker，健康状态变为 unavailable，正常提交返回 HTTP 503。核对原项目资源归属后删除本次自有容器、网络和两个命名卷；私有安装目录及报告保留。原数据卷已经清理，不能直接用该验收目录启动一个替代空站点。证据在 `test-results/postgres-compose-2026-11-16/attempt-1/`，结束于 2026-11-15 18:03:54 UTC，全程未访问 GPU。

## 配置解析问题与后续范围

环境文件的反斜杠重复转义曾在固定 CPU 容器实际复现，原失败保留。改为 Compose 双引号字符串及显式美元符转义后，六种引号、反斜杠、美元符和中文值在容器内逐字相同；四种 CPU/GPU/AI 配置组合另行经实际 Compose 解析。证据为 `compose-dotenv-before-2026-11-16.json`、`compose-dotenv-after-2026-11-16.json` 和 `compose-preparation-parser-2026-11-16.json`，均在 `test-results/`。

随后只读复现宿主变量覆盖准备镜像的问题，按 [ADR-098](adr/098-frozen-compose-model.md) 固定解析后的私有 Compose 模型。53 项文件、配置和拒绝检查通过，实际解析往返、冲突宿主变量、特殊字符路径和独立 `gpu-check` profile 均通过；这批 GPU 配置为未挂载的合成输入，不证明设备成功。

第二轮独立新站点使用 `503c7de` 的公开准备入口和生成的 `compose.sh`，在调用者设有冲突镜像、端口、项目、文件和 profile 环境变量时运行完整 CPU/AI 流程。实际使用的仍是原固定模型：首次健康 173.304 秒、真实 AI 浏览器 16.841 秒、原卷重建 163.886 秒。七题型、Run/错解、55 表/33 序列、原会话/私有草稿、撤回目录、重建后正确提交及离线 HTTP 503 均通过；自有容器、网络和两个卷已清理。记录在 `test-results/postgres-compose-2026-11-16/attempt-2/`，结束于 2026-11-15 18:53:40 UTC，全程未访问 GPU。原第一轮记录保留。

<a id="gpu-compose"></a>

## 实际 PostgreSQL、GPU 与 AI 组合

同一应用包与准备工具另建独立站点 `72a824e0640d4f888cda4c025357d055`，只使用获授权的 RTX 3090 / GPU 0。准备两次分别为 10.279 秒和 9.128 秒，未启动服务；一次 Compose 命令启动 PostgreSQL、初始化服务、Web、CPU worker、GPU worker 和本地 CPU tutor。冲突调用者环境没有改变固定镜像或启动独立检查 profile。

首个 GPU worker `22f630b01bb38341f18848a6ab1add6f99b96829c3423fc853f296edabf332d2` 使用冻结镜像 `sha256:3e522f8eab430aab506c3597e0d4b04a9add72296e8ab5b07edd9fb67000828c`，从 2026-11-15 19:18:54 UTC 启动，到 19:33:14 UTC 观察到全部 24 阶段通过及新鲜就绪。该镜像的执行策略、控制器与任务镜像沿用之前已验收版本；应用和 bootstrap 增量经这次实际硬件完整检查。

仅在这个新建合成站点发布包含两道 CUDA 候选题的完整内容包，固定管理容器没有设备或 Docker socket。七种 CPU 题型、Run/错解通过；向量加法和矩阵乘法经正常版本化 API 共完成十四项任务：八项 accepted、六项针对性 wrong_answer，含两次跨账号正确 Submit。每个账号预约 1170 秒，默认额度未放宽。匿名读取私有结果为 401，另一个账号为 404。实际本地 AI 浏览器流程也通过，合成浏览器账号已删除。发布只影响此测试站点，仓库默认目录仍为 26 道 CPU 题。

随后保留原卷执行 `down` / `up`。初始化容器确认原数据库而未重新导入；PostgreSQL、Web、CPU worker 和 tutor 均健康。但 GPU 0 出现 UID 2048 的外部交互进程，原重建 worker `462da02819a7ae5926bc6a88eee17bcffd1e8264f04811e0509eb4c3c8718d35` 以 `SandboxUnavailable` 退出，未停止外部进程。原尝试保持失败状态；未把首次启动或数据保留当作 GPU 重建成功。

19:56:12 UTC 的独立只读审计确认，重建前后 55 张业务表的 575 条记录摘要一致，33 个序列不回退；GPU 设备为 quarantined，没有物理预约、活动 journal 或未完成提交，CPU 仍可用。保留原站点、私有配置、数据卷和这个已退出的 worker，等待设备空闲后从同一对象恢复。完整请求/结果、首次启动及原失败在 `test-results/gpu-compose-2026-11-16/attempt-1/`，数据审计为 `busy-recreation-audit.json`；不公开其中的账号、令牌、配置与受保护测试文件。
