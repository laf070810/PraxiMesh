# G19 CUDA 内容校验与候选验收工具

日期：2026-10-31。实现前基线：`main` 的 `a3e4618`。本批继续执行维护者“不占 GPU”的要求；没有编译或执行 CUDA，没有启用设备、签发运行凭证或修改已运行的站点。

## 交付范围

- 内容 v1 接受 `cuda-kernel-v1`，复用 GPU 数据契约和 UTF-8 源码预算；拒绝 CPU/GPU 混合字段，不放宽七种 CPU 契约，不把 CUDA 加入 CPU 分派表。
- 两道候选提供独立题意、公开与受保护测试、参考解和各三种针对性错解；新增六个知识节点并引用原有向量、矩阵及矩阵乘法概念。默认目录仍为 50 节点、26 道题。
- `content_compose` 仅追加新 ID，生成完整、私有、不可覆盖的 v1 包。正式导入仍保留版本不可变、组织归属和全量撤回规则。
- `scripts/gpu_content_acceptance.py` 通过普通 API 顺序提交参考 Run、正式参考、错解和恢复参考；核对固定内容与环境身份、已消耗预约和完整正式反馈。未知提交结果及观察超时保留请求键和任务，不自动重发或清理站点。

内容规范见 [CUDA 契约](cuda-contract.md)，操作统一见 [GPU 指南](deployment/gpu.md#cuda-content)，决定见 [ADR-035](adr/035-cuda-content-and-review.md)。

## 实际验证

| 检查 | 结果与范围 | 原始记录（忽略目录） |
| --- | --- | --- |
| 旧默认内容基线 | 21 项通过；默认 50 节点、26 题 | `test-results/g19-content-baseline.xml` |
| 内容、候选及组织发布 | 85 项通过；包括混合字段、UTF-8 字节、错误预期、不可变版本、命名空间和撤回 | `test-results/g19-content-initial.log`、`.xml` |
| PostgreSQL 发布与权限 | 87 项通过；后续新增的公开投影/关闭准入检查另 1 项通过。两个自有临时容器与卷均已清理 | `test-results/g19-postgres.log`、`g19-postgres-public.log` 及对应 XML |
| API 验收工具协议边界 | 17 项通过；仅使用明确的协议替身，验证摘要、身份、私有日志、错配中止、失联不重发和观察超时 | `test-results/g19-api-tool-initial.log`、`.xml` |
| 内容、工具、真实应用 API、发布及初始化回归 | 181 项通过、1 项跳过；导入候选后的网页与公开 API 可读，隐藏测试不泄露、GPU 准入返回 503，未创建任务或设备 | `test-results/g19-components.log`、`.xml` |
| 上述唯一跳过项的真实 CPU 重放 | 显式配置可信 WASI 后 1 项通过；正确及错误公开程序实际执行，结果仍非正式成绩 | `test-results/g19-real-cli.log`、`.xml` |
| 文档命令 | 在新建临时 SQLite 库重放组合、检查、迁移及导入；得到 56 节点、28 题、0 设备、0 提交；输出 0600，临时库已删除 | `test-results/g19-docs-replay.json` |

最后的组合包规范摘要为 `af4c860fa8d0763d4d2837b89d9797b87edbe24c285c1b558d203aace1b719a0`，保存在 `test-results/g19-content-final.json`。该包只用于临时库检查，未导入用户站点。

全仓 Ruff 检查、Python 文件格式检查、迁移无变化检查及 `git diff --check` 通过。674 个本地文档链接有效，记录在 `test-results/g19-document-links.json`。初次 Ruff 检查发现跨测试文件复用 fixture 的两处 F811 导入告警，改用显式重新导出后通过；未以忽略检查解决。审阅时也将候选题面的浮点比较公式改为实际使用的 `math.isclose` 规则，再生成并校验最终包。

执行代码未改变，现场重算的 CPU 策略摘要仍为 `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，GPU 摘要仍为 `26de9b4fa013aa197d54fb43bb61d67224603f0a2743250cb950e0f40594eaa3`。真实 WASI 使用 G18 Compose 产出的可信 bundle；不是复用旧运行凭证。没有重跑完整 Compose/浏览器矩阵，本批未改动界面或部署入口。

## 仍待完成

候选源码尚未在当前完整 GPU worker 上编译、执行或验证性能；API 工具本批只有协议及普通应用边界证据，没有真实 GPU 队列通过报告。必须恢复独占硬件条件，完成二十四阶段启动、所有参考解与错解、恢复、预算、实际队列及部署验收后，才能决定发布候选。容器化 GPU 部署、本地 GPU 执行和可比性能榜单仍是独立未完成项。默认设备名单和题库均未开放这些候选。
