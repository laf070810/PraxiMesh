# RFC-016：可直接做题的 Compose 单机部署

状态：实施中，2026-10-30。承接 [RFC-015](015-compose-preview.md)。

## 问题与决定

维护者要求一键启动完整的已实现功能。现有入口仅启动 Web，缺少 worker、可信解释器制品和模型配置，不能完成实际做题闭环。

默认 `docker compose up --build -d` 启动 Web 和独立 WASI worker。构建阶段从现有固定摘要的 CPython、QuickJS、WASI SDK 和 Wasmtime 来源生成联合 bundle；不要求宿主 Python、预先存在的 `var/` 或手工填写 bundle 摘要。Web 镜像只保存构建得到的摘要，worker 镜像携带同一 bundle；不让 Web 安装执行运行时或挂载 Docker socket。

沿用 `app-data` 卷及 SQLite，保留当前体验实例的账号、密钥、会话、草稿、目录和历史。此选择避免一次启动隐式迁移数据库引擎。功能覆盖现有七种 CPU 评测器、项目、学习、社区、组织、挑战与多语言；生产 PostgreSQL 角色、HTTPS 及代际升级仍沿用既有发布协调器。GPU 未实现的边界不变。

## 启动、健康与恢复

- 新启动器复用已验证的原子新库初始化与旧库仅迁移路径；旧页面体验入口继续拒绝评测配置。worker 等待 Web 数据库初始化完成，不执行迁移或内容导入。
- worker 无网络、非 root、只读根文件系统、受限内存/PID，使用既有 WASI guest 能力、fuel、CPU、地址空间和墙钟限制；共享 SQLite 只在可信控制器中使用，guest 不获得数据库或平台凭证。
- 每次 worker 启动真实执行 `wasi_service` 的完整隔离探针。配置相同的策略/制品及健康记录才开放提交。探针失败或 worker 离线时页面仍可阅读，提交关闭。
- receipt 仍为 24 小时有效。到期 worker 退出，Compose 重启时重新执行完整探针，期间不接收新提交；不自动延长旧 receipt。服务健康检查检验本 worker 的心跳与匹配指纹。
- `down` 保留数据；普通重建不重导内容。演练先使用独立 Compose 项目和合成数据，再保留当前实例卷升级。正式运行中的 schema/策略升级继续遵循备份与排空文档。

## 可选 AI

Web 可读取操作者配置的既有 HTTPS 模型服务；凭证只传给 Web。无模型配置时规则提示、计划及正式评测正常工作。

增加 `compose.ai.yaml`，一条 Compose 命令同时构建并启动已验证来源的 CPU llama.cpp / Qwen3-0.6B 模型。模型不默认下载，由明确选择该文件触发。模型服务与 Web 共享网络命名空间，绑定数字回环地址，复用现有显式本地 HTTP 支持；不发布模型端口。独立私有卷保存随机模型令牌，只挂载到 Web 与模型容器，不授予 worker。模型容器没有数据库卷，不使用 GPU、工具或联网代理。模型及源码下载校验固定摘要，失败不启动替代服务。

Compose 的依赖、网络命名空间和健康语义以 [Docker 官方文档](https://docs.docker.com/reference/compose-file/services/) 为依据。采用可选覆盖文件而非把模型作为基础依赖，避免模型下载或故障影响真实评测。小模型输出仍是需要核对的建议，不构成判题依据。

## 验证计划

验证首次构建无需宿主制品、完整真实探针、HTTP 的 Run/Submit 正确和错误结果、各 CPU 题型、worker 停止后的关闭状态与恢复、重建后的历史/会话/草稿/目录保留。检查容器权限、网络、无 Docker socket、Web 无 Wasmtime、模型凭证不传 worker。重放可选模型真实提示及既有浏览器功能。记录原始失败和修复结果，清理测试自有资源；不把历史验收算作此次复测。
