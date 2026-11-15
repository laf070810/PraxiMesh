# PostgreSQL Compose 的独立验收

更新：2026-11-16。固定源码 `7b4fb25` 的 PostgreSQL、Web、CPU/WASI worker 和本地 CPU AI 已在独立新站点通过实际 Compose 启动与同版本重建。GPU Compose、固定配置启动脚本的后续实测和跨版本维护仍待完成；不能将本页作为 GPU 就绪证明。操作集中在[PostgreSQL Compose](deployment/compose-postgres.md)。

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

随后只读复现宿主变量覆盖准备镜像的问题，按 [ADR-098](adr/098-frozen-compose-model.md) 固定解析后的私有 Compose 模型。实际解析往返、冲突宿主变量、特殊字符路径和独立 `gpu-check` profile 均通过；GPU 配置为未挂载的合成输入，不证明设备成功。固定启动脚本还需实际新站点验证，之前的原环境文件启动证据不改写。
