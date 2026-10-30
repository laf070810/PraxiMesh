# Compose 单机部署验收

日期：2026-10-30。范围为 [RFC-016](rfcs/016-compose-selfhost.md) 的 Web、真实 CPU/WASI worker 与可选本地 CPU 模型，使用 Linux x86_64、Docker Engine 29.9.2 和 Compose 5.6.1。基础组合与带模型组合均已通过完整容器验收。使用方法见 [Compose 指南](compose.md)。

## 已验证结果

- 70 项初始化、服务配置、下载失败处理及发布兼容性检查通过，无跳过；证据为 `test-results/compose-full-unit.xml`。
- 基础组合由镜像构建可信解释器，不读取宿主 `var/` 制品。worker 首次启动通过全部 129 个真实探针，无失败重启；七种评测器的参考程序经 HTTP 正式提交全部 Accepted，错误程序为 Wrong Answer，公开 Run 也通过。
- 默认绑定 `0.0.0.0`，通过独立测试端口的服务器 IP 访问页面、静态资源、注册及练习入口；同源保存草稿成功，错误 Origin 被 CSRF 拒绝。Django 调试详情关闭。
- Web 不包含 Wasmtime 或 Docker socket；worker 无网络且不接收模型配置。容器使用非 root、只读根文件系统及内存/PID 限制。
- 停止 worker 后，心跳失效使新提交返回 503，阅读仍可用。重建后重新运行真实探针，账号、会话、密钥、私有草稿、正式成绩、自定义知识点及撤回状态均保留。
- 基础组合结果为 `test-results/px-compose-982730abd361/result.json`；`passed` 与 `owned_resources_removed` 均为 true。测试容器、网络与卷均已清理。
- AI 组合中的四组浏览器流程已通过：注册、草稿、真实 Run/Submit、私有笔记与社区；四个项目阶段阅读及英文回退；类/API 的本地 CLI、幂等正式提交与公开撤回；真实模型提示、输入预览、源码默认不发送、跨账号隔离、导出删除和正式成绩不受 AI 影响。合成账号完成删除。
- AI 组合也通过 129 个启动探针、七种真实正式提交、错误提交、公开 Run、离线关闭及重建后的数据/会话/历史保留。模型健康启动且无失败重启，只挂载只读凭证卷，不挂数据库或发布端口。结果为 `test-results/px-compose-e8bf0f80f8d9/result.json`，四组浏览器结果位于同目录；`passed` 与 `owned_resources_removed` 均为 true。

真实模型对“零是否应计入正数和”的公开问题返回了逐步累加与严格正值判断的建议。该次只证明真实请求、界面与隐私流程完成，不代表提示质量或模型准确率通过评测；历史错误建议和小模型限制仍保留在模型指南中。

镜像内联合 WASI bundle 的 SHA-256 为 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`，策略摘要为 `f7a955218ed9b662a2f878949f11014ed77bc84faefae9e6dfac6c6b9db5608a`。解释器为 CPython 3.14.9、QuickJS 2026-06-04、Wasmtime 50.0.1；完整源码身份与探针 suite 摘要保存在结果文件中。模型和 llama.cpp 的固定来源见 [AI 指南](local-coaching.md)。

## 原始失败与修复

1. 首次下载因构建未继承宿主代理而超时；补充 Docker 预定义代理参数及有界、校验后发布的下载。CPython 交叉编译随后因缺少 Git 失败，补齐工具后构建成功。日志依次为 `compose-full-build.log`、`compose-full-build-retry1.log`、`compose-full-build-retry2.log`。
2. 初次真实隔离检查中，Agent 正确程序触及单轮 CPU 限制。相同容器中的新旧 bundle 均复现，冻结 F 镜像也接近边界。保持原策略和题目预算，使用同版可信 CPython 在构建时缓存标准库字节码；优化后连续五次真实 Agent 运行通过，再通过完整探针和题型验收。失败项目为 `px-compose-7931940ae7da`，诊断日志为 `compose-agent-diagnosis.log` 和 `compose-agent-bytecode-diagnosis.log`，最终构建日志为 `compose-full-build-bytecode.log`。
3. 首次 AI 组合因下载文件权限使非 root 模型进程无法读取权重而失败。仅将镜像中公开权重及许可证设置为 `0444`，私有模型令牌权限不变。失败项目为 `px-compose-9671e03fc9a5`，直接诊断日志为 `compose-tutor-diagnosis.log`。修复后的完整组合验收通过。
4. 权限修正后的模型健康启动、129 个真实探针通过，随后宿主浏览器因未指定已安装的 Chromium 缓存而启动失败。保留 `px-compose-0675076a3b3a` 结果，设置 `PLAYWRIGHT_BROWSERS_PATH` 后实际验证 Chromium 启动；验收入口增加浏览器前置检查，避免在长时间构建与隔离检查后才暴露此类环境缺失。

上述日志与项目目录均位于忽略的 `test-results/`。失败测试的自有容器、网络与卷也已清理；所有重放使用独立数据库，没有改写冻结 F 制品或操作用户数据库。

## 验证边界

本次为单机 SQLite/HTTP 入口的真实容器验证，不替代生产 PostgreSQL/HTTPS 发布协调器、目标站点持续容量或托管 CI 的验收。没有改动已发布题目、迁移或评测策略，也没有重新执行整个历史全回归矩阵。GPU 评测、公开运营和项目许可证决定仍按[需求追踪](requirements.md)保留。
