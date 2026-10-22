# RFC-001：阶段 A 的可学习、可练习、可验证闭环

状态：接受。2026-10-22 初版。关联：[PRD](../../PRD.md)、[计划](../plan.md)、ADR-001–003。

## 问题与选择标准

仓库最初只有 PRD。首要目标是基础在线练习的可信结果，同时控制维护成本、账户风险和贡献门槛。先做模块化单体与独立执行边界，不建立多套学科网站。

比较：Django LTS 自带 ORM、迁移、会话、表单、CSRF 和密码框架；FastAPI + SPA 需自行组合这些能力；全栈 JS 可行但目前没有独特收益。选择 Django 5.2 LTS + Python 3.11 + 模板/少量原生 JS，避免前端构建链成为入门前提。SQLite 用于本地单 worker，PostgreSQL 用于生产队列并发；用相同 ORM，CI 必须测两者。前期无需 Redis/Celery，原子领取和租约足够；工作量扩大后可替换调度器。

## 内容、身份与版本

JSON 内容包使用 JSON Schema，仓库审核后导入。Domain/Topic 提供导航；Concept 拥有跨领域归属、稳定 slug、别名、学习目标；Relation 区分 required/recommended/related，required 无环。Path 是独立的教程/练习序列，不是知识 DAG。抽象知识不绑定 Python；具体题目声明语言和 evaluator。

题目身份稳定；每次修订增加正整数 revision。版本保存完整题意、公开样例、私有测试、参考解法、许可来源和变更说明，内容 digest 唯一标识。相同版本禁止变更；历史提交锁定版本。下线通过 published=false，保留旧版本。新增题型由版本化 evaluator 注册表承接；不允许投稿任意导入 Python 插件或上传可执行 checker。

## 信任边界与结果语义

Web 只存储/排队，不能访问容器 socket。Worker 在专用节点读取任务，使用可信控制器逐用例调用沙箱，预期值从不进入执行容器。初期支持 `python-function-v1`：JSON 参数，JSON 结果，严格类型比较或显式浮点容差。公开 Run 只用公开样例且不计进度；Submit 用全部测试。一个 Accepted 只说明该题该版本通过，绝不展示“掌握概念”。

候选隔离：普通 Docker 与宿主 subprocess 不满足发布门槛；gVisor + cgroups 保持标准 CPython 兼容性，最初选择为阶段 A 后端；microVM 隔离强但增加初期运维；Wasmtime/WASI 的基础函数范围随后在 [RFC-002](002-wasi-python.md) 完成专项验证，并由 [ADR-004](../adr/004-wasi.md) 接受；gVisor 策略保留。Sites 的 Workers 托管无法提供此 Linux 判题边界，因此采用仓库内自部署应用，不绑定专有平台。

任务 queued → running → finished，终态 verdict 为 accepted/wrong_answer/syntax_error/runtime_error/time_limit/memory_limit/output_limit/internal_error。内部错误不记为用户错题；失联任务结束为 internal_error，不自动重复执行。通过量仅计算正式 accepted。当前输入在容器中不可避免可见；不宣称开源静态测试能抵御预知数据的作弊。未来需要保密测试时，私有包应独立交付，不能提交公开仓库。

## 安全、成本与失败

gVisor 只提供一部分边界，必须配置 cgroups、网络、文件系统、权限及输出限制。每个用例新容器，牺牲冷启动速度换取不同用例状态隔离。默认关闭，合格运行时 + 探针 + 心跳才接收提交；安全回归通过前不公开提供执行。上限从小规模教学试用开始：每用户少量待处理任务、全局有界队列、单 worker、固定 CPU/内存。后续依据实测延迟调整，而非先承诺吞吐。

账号使用 Django 会话和 Argon2，初期用户名 + 密码 + 一次性恢复码，不收邮箱、不接第三方身份。数据导出和删除需要登录，删除再次验证密码。公开社区身份和成绩另行明确 opt-in 规则。恢复码遗失且无会话时不可绕过身份验证恢复。

## 交付、迁移与验证

锁文件，非 root 应用容器，独立 judge 节点，TLS 反向代理。静态网页资源自托管，不引入外部字体/脚本。单元测试覆盖比较、内容、队列和权限；真实沙箱测试覆盖攻击行为；浏览器验证注册→练习→历史；CI 构建和依赖审计，公网发布必须另过沙箱与运营门槛。见运维手册中的备份恢复和停用流程。

以后拆调度器、追加身份提供商或新 evaluator 时保留题目/概念 ID 和历史版本；迁移必须提供回滚或兼容策略。当前本机 Docker 没有 runsc，gVisor 的验收仍未完成；基础函数的 AC-04 由独立 WASI 后端的实际测试提供证据，不能互相代替。

## 调研依据（2026-10-22 核验）

- [Django 支持周期](https://www.djangoproject.com/download/#supported-versions)：选择仍受维护的 LTS；具体补丁以锁文件及包源为准。
- [Django 安全说明](https://docs.djangoproject.com/en/5.2/topics/security/)与[部署检查](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)：应用层防护仍需配置和测试。
- [gVisor 安全模型](https://gvisor.dev/docs/architecture_guide/security/)与[Docker 集成](https://gvisor.dev/docs/user_guide/quick_start/docker/)：系统调用边界，资源限制依赖宿主。
- [Docker 资源限制](https://docs.docker.com/engine/containers/resource_constraints/)：默认没有资源限额，必须核验宿主能力。
- [CPython WASI](https://github.com/python/cpython/blob/main/Platforms/WASI/README.md)：未来候选，未据此声称现有评测已安全。
