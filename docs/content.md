# 内容规范与贡献审核

当前格式：`content/schema.json`（版本 1），完整示例：`content/starter.json`。内容作为数据加载，不执行参考代码或动态插件。`content_check` 是结构检查，**不是解法通过证明**。

## 知识组织

稳定 ID 使用小写 ASCII slug，不因标题、翻译或归属变化改 ID。Domain/Topic 表示导航，Concept 可关联多个领域/主题；tutorial 引用概念；path 用显式 steps 引用教程或题目。抽象概念避免语言前缀；题目按评测契约声明 Python 或 JavaScript。required 关系表示必要前置，recommended 表示建议，related 表示横向关联。只检查 required 无环，相关关系允许环。重复、指向自身或同目标的矛盾关系禁止。

新增概念前搜索标题和 aliases。重复概念应先 RFC 讨论合并并保留旧 ID 的迁移，不直接删除身份或修改历史题目。标题/别名唯一性在同一包内检查。界面支持简体中文与英文；内容译文绑定同一稳定 ID 和精确源摘要（题目还绑定不可变版本），缺失译文回退中文并明确标记，不能靠复制概念翻译。导入与回退规则见[本地化指南](localization.md)。

## 一道题的完整契约

每题必须有任务、输入/输出/约束、目标、知识点、难度、语言、起始代码、参考实现、提示、公开和受保护测试、比较规则、修订说明、署名和授权来源。整个内容包最多 5 MB；用例应覆盖普通输入、声明上限、关键边界和典型错误。当前七种评测器的调用与测试数据不同：

| evaluator | 语言与执行契约 | 正式测试数量 |
| --- | --- | --- |
| `python-function-v1` | Python；声明 `function`，用例为 JSON `args` 与 `expected` | 4–20 |
| `javascript-function-v1` | JavaScript；同步 JSON 函数，声明 `function`，用例为 `args` 与 `expected` | 4–20 |
| `python-stdio-v1` | Python 完整程序；声明 `output_format: "json"`，用例为 `stdin` 与 `expected`，不声明 `function` | 4–20 |
| `python-object-api-v1` | Python 类；声明 `interface`，用例为构造/方法 `operations` 与逐项 `expected`，场景内保持实例状态 | 2–20 |
| `python-ml-linear-v1` | Python 训练函数；接收训练特征/标签，返回权重与偏置，控制器在独立留出数据上计算 MSE/MAE | 2–8 |
| `python-weighted-fit-v1` | Python 拟合函数；接收 x/y/sigma，返回参数与协方差，控制器核对加权拟合与公开诊断 | 2–8 |
| `python-agent-tools-v1` | Python `act(observation, state)`；固定账本工具与资源策略，控制器验证实际动作和最终报告 | 2–8 episodes |

所有正式题目必须同时含公开与受保护场景，测试名称不能重复。函数和完整程序用例的输入 UTF-8 JSON 最多 100,000 字节，预期结果的 ASCII 转义 JSON 最多 32,000 字节；`numeric` 必须显式声明绝对/相对容差。专业题型使用各自严格的数据、指标和资源字段，不能上传 checker 或自报分数；ML/科学数据集声明稳定 ID、revision、seed 与内容 digest。所有类型拒绝非有限数值。

完整字段和边界以 [JSON Schema](../content/schema.json)、[ML 验证器](../judge/ml.py)、[科学计算验证器](../judge/scientific.py)为准。调用、反馈与资源语义见[WASI 指南](deployment/wasi.md)、[完整程序契约](stdio-contract.md)、[类/API 契约](object-api-contract.md)、[Agent 契约](agent-contract.md)和[专业评测设计](rfcs/010-specialist-evaluation.md)。七种评测器均支持 WASI；保留的 gVisor 后端只支持 Python JSON 函数。

Python/JavaScript 函数模式只支持 JSON 参数和 JSON 返回值。题页显示实际后端支持的标准库边界，无网络或第三方包。JSON 不表达 tuple、集合、自定义对象。`exact` 要求数值类型一致（布尔不等于整数）；`numeric` 允许 int/float 在容差内相等，仍检查形状、键和有限性。

公开页面通过字段白名单生成，不包含 reference、私有测试名称/输入/输出。受保护测试每次只向沙箱提供当前输入，控制器持有预期值。**示例包在源码仓库中可见，因此其测试不是对仓库读者保密的考试数据。** 部署时不要将内容目录配置成静态目录。真正需要保密的测试包应放在私有发布源，并另行评估泄露与作弊风险。

## 修订与发布

1. 修改 JSON 包，执行 `uv run python manage.py content_check path/to/package.json`。
2. 审核知识语义、关系、文字可独立理解性、许可来源；人工核对样例。不能凭 schema 通过就标记“已审核”。
3. 在隔离的测试 worker 上运行所有参考实现和典型错误变体。内容贡献同样不可信，不允许在 Web/CI 宿主直接 `exec(reference)`。
4. 官方内容由维护者审核后运行 `content_import`，在一个事务内替换未由组织包管理的内容：该范围内缺失 ID 会下线，旧记录及题目版本保留。导入拒绝覆盖已归属组织包的 ID，并在全量撤回时排除这些 ID。
5. 已存在的 `(problem_id, revision)` 不可修改；更新题意、测试、环境契约或参考实现都增加 revision 并写 changelog。历史提交保留旧版本数据和摘要。导入旧版本不能自动回滚当前版本。

第三方包经组织投稿、独立权限审核和精确摘要批准后，由可信维护者运行 `org_package_publish` 发布；`org_package_withdraw` 只撤回该包拥有的内容。不能以官方导入替代这个工作流。稳定命名空间、外部引用 allowlist、版本不可变、组织课程固定版本和个人进度隐私规则见[组织与题库指南](rfcs/009-organizations-and-packages.md)。

路径进度统计当前版本的正式通过。公开样例运行不算完成；旧版本历史仍可查。撤回路径或题目不会删除历史。正式版权/许可和独立审核尚未完成前，内容包的 `LicenseRef-Pending-Maintainer-Approval` 表示内部开发状态，不能当作可再许可素材。
