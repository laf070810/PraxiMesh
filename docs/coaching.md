# 私有学习规划与可选模型提示

学习规划无需模型服务。登录后进入 `/coaching/`，选择最多八个公开概念作为私有目标；规则计划按公开前置关系和本人可信正式结果生成，最多 24 个固定题目版本。可以跳过、恢复、关闭或删除建议。重新选择目标会新建计划，原计划保留为私有历史。目标 source digest 和题目版本不随内容更新改写；内容撤回后停止显示正文，仍可删除本人元数据。

多维页面检查最近最多 500 条已完成正式记录，按不同题目、版本、题型、难度、概念、领域及自选目标展示证据，明确显示统计范围。重复提交同题不会增加题目多样性；历史版本通过不代表当前版本通过。再次失败和超过三十天未练习的信息单独保留。它调用统一 `core.result_evidence.verified_evidence`，没有简化的 Accepted 字符串捷径，不计算掌握百分比或总能力分。当前提交历史仍可查看更早记录。

## 可选提供方

默认 `COACHING_ENABLED=0`。启用需要维护者配置固定提供方、模型、公开说明和凭证；用户不能传入服务 URL、请求头或工具。配置均只交给 Web，不交给 worker 或管理容器：

- `COACHING_ENABLED=1`
- `COACHING_ENDPOINT`：固定 HTTPS `/v1/chat/completions` 地址。
- `COACHING_MODEL`：维护者选定的模型标识。
- `COACHING_TOKEN`：私有服务凭证，不进入 argv、用户回执或导出。
- `COACHING_PROVIDER_DESCRIPTION`：用户发送前可读的提供方及数据处理说明，不能为空。
- `COACHING_CA_PEM`：可选私有 CA PEM；与 `COACHING_CA_FILE` 互斥。自部署协调器使用内嵌 CA，避免任意宿主文件挂载。
- `COACHING_MAX_TOKENS`：默认 192，上限 256。
- `COACHING_DAILY_LIMIT`：默认每用户 UTC 日五次，可配置 1–100。
- `COACHING_CONCURRENCY`：默认全局一槽，可配置 1–4。
- `COACHING_DISABLE_THINKING=1`：可选的兼容服务配置。

显式本地开发才设置 `COACHING_ALLOW_LOOPBACK_HTTP=1`，只允许数字回环 HTTP；生产协调器仍仅接受 HTTPS 提供方。应用的独立传输进程硬截止 20 秒，固定 JSON 契约，拒绝重定向、环境代理、工具调用及超限/格式错误响应。失败不会生成替代提示或修改正式评测结果。

## 明确同意与私有记录

每次先生成输入预览，再提交确认。预览只包含题目公开说明、约束、公开样例、用户问题及本人主动选中的作者提示。源码默认不发送；用户只有勾选明确同意并主动粘贴片段才会把该片段加入预览。应用不自动读取草稿、提交源码、隐藏测试、参考实现或他人资料。发送的用户 JSON 和系统指令就是预览展示的内容，绑定请求摘要、提供方配置摘要和提示版本。

调用按 UUID 幂等。数据库锁内预约全局并发槽和 UTC 日配额，再出事务调用模型；已外发后失败或结果不确定仍保守计额。删除会话不会重置当日次数；账号删除级联清除目标、计划、会话和个人用量。执行中删除不会重新创建正文。无正文、无用户身份的短期执行租约保留到完成或 25 秒截止，避免删除动作绕过并发限制；其他请求和账户页面不需要等待模型事务。

输出按不可信纯文本显示，不执行代码、不调用工具、不写分数。模型可能错误，正式结果仍由可信评测决定。实际合成浏览器测试证明了一次公开“严格正数求和”问题返回可用概念提示；它不证明模型对所有主题可靠，已有独立模型测试发现统计学解释会出错，因此不能把小模型当作可靠默认或唯一裁判。

## 复现

应用/边界单元测试可使用替身验证隐私、超时及失败；替身不证明真实模型可用。

```console
python -m pytest tests/test_coaching.py tests/test_coaching_provider.py -q
```

PostgreSQL 原子预约和执行中删除测试使用独立可销毁的 owner 数据库：

```console
DATABASE_URL=... DB_SSLMODE=disable python -m pytest tests/test_coaching_postgres.py -q
```

默认无需模型运行 `python scripts/coaching_acceptance.py`，验证私有目标/版本计划/证据空态/导出删除及 AI 禁用状态，收据 actual_model_request=false。真实浏览器脚本不修改数据库状态、不伪造提示或判题结果。需要 AI 时维护者先启动固定模型，再提供 owner-private 0600 JSON 配置文件，其字段对应 `ProviderConfig`（endpoint/model/token 等），执行：

```console
PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers python scripts/coaching_acceptance.py --provider-config /private/model-connection.json
```

启动器只创建临时本地 Web/数据库和合成账号，测试真实模型调用、预览默认排除源码、私有导出、计划/会话删除及账号删除；随后停止自己启动的 Web 并移除临时数据库，不管理模型服务。既有 HTTPS 演练可直接设置 `BROWSER_URL`、`BROWSER_COACHING=1`，必要时沿用回环 `BROWSER_TEST_SPKI` 精确证书固定，再运行 `tests/browser_coaching.py`。不要通过忽略全部 TLS 错误运行。

收据和截图位于 `test-results/coaching-browser.json`、`coaching-real-model.png`、`coaching-launcher.json`。浏览器收据只保留公开合成问题的回答用于质量审阅，不保留账号凭证、私有导出或真实用户内容。公开部署、外部提供方政策和项目许可证由维护者后续决定，不阻止本地可验证自部署。
