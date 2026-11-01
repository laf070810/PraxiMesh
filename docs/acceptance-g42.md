# G42：四镜像 GPU 发布制品

2026-11-01，本批制品、导入和非 GPU 应用验收完成。设计见 [ADR-058](adr/058-gpu-release-artifacts.md)，操作集中在[发布指南](deployment/releases.md#gpu-release)。本轮不查询、映射或使用 GPU，不改变现有运行站点。

## 已实现

可选 format 2 将 Web、CPU worker、可信 GPU worker 和单卡任务镜像绑定到同一源码快照。构建使用冻结副本，归档保存驱动、完整管理制品和有序源码契约；验证先检查可信摘要、文件与镜像实际层，再核对各角色、入口和契约。验证不执行归档代码，不依赖当前工作树或缓存层。默认 CPU format 1 与旧包读取继续保留。

导入后的无设备 worker 检查使用同一实际 PostgreSQL/Compose 场景，新增默认入口关闭检查。发布浏览器验收支持新的独立输出目录，避免覆盖既有记录。协调器明确拒绝尚未接入 GPU 生命周期的激活，拒绝发生在镜像导入、排空、数据库或代理变更之前。

## 验证记录

`test-results/g42-baseline.json` 记录原读取器接受旧格式、拒绝新 GPU 格式。旧 F 归档的实际成员与标签结构已核对。首轮旧格式/部署回归 **85 passed**；加入 GPU 归档与拒绝检查后 **121 passed**，增加非规范路径检查后 **122 passed**。测试中的镜像层是明确的不可执行夹具，不表示真实镜像或 GPU 通过。

实际四镜像构建、完整归档校验和导入已通过。三份制品的 CPU/WASI 发布流程均通过；第三份同时通过修正后的完整输出目录保护检查。各轮覆盖同一组应用流程，不把重复执行相加为独立测试数量。

补齐畸形 JSON 类型拒绝后，最终相关组件检查 **127 passed、0 skipped**，见 `test-results/g42-artifact-complete.log` 和 `.xml`。新读取器已按 F 验收中记录的清单摘要 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` 核验实际旧包，464 个源码文件、两个镜像及完整层通过，见 `g42-f-compatibility.json`。这只重放旧制品读取，不将 F 的历史部署结果算作本次重跑。

浏览器前置检查首次因默认缓存路径不存在失败，记录在 `g42-browser-preflight-default.log`；指定已安装的独立缓存后启动通过，见 `g42-browser-preflight-fixed.log`。没有安装或改变服务依赖。该前置检查只验证浏览器启动，页面流程由下方真实验收验证。

首轮镜像浏览器执行时发现新输出目录只作用于父进程日志，未传给主要浏览器子进程；七个截图或子报告仍写到公共 `test-results/`，原始身份见 `g42-browser-output-first.json`。已补齐显式目录的传递；默认未指定目录时保持原行为。首轮继续跟踪到终止并保留结果，随后使用新发布目录和新验收目录重放，不原地改写已经完成的制品。

第二轮复核发现英文流程和本地 CLI 流程仍将目录写死，忽略已经传入的 `BROWSER_ARTIFACTS`；英文截图和两个子报告继续写到公共目录，原始输出保护断言失败，实际重现日志为 `g42-output-protection-second.log`。已让这两个脚本沿用其他浏览器入口的目录选择，未设置变量时保持原路径；再生成第三份不可变制品验证全部子记录。第二轮的产品流程和输出目录保护分别记录，不把部分路径修正描述为全部通过。

## 实际制品与无设备应用

首轮不可变发布目录为 `var/releases/2026-11-01-g42-candidate-1`，构建日志为 `test-results/g42-build-first.log` 和 `release-build-7095bb1cd39b.log`，GPU 子构建日志位于同名 `.gpu/` 目录。构建完成后未改写该目录。

| 身份 | SHA-256 |
| --- | --- |
| 发布清单 | `76964d60385dd3e56a3166038a7af2a1293f582331b6508e341f52cd8655c20d` |
| 冻结源码 | `e2de5196fe7a939f15cb4c12e6f852756da3250b7e6461c7bc8bb724f8d70f42` |
| Web 镜像 | `b48f4efe277da4d0bfa3612b419f76a4cd7786f135b5250e7f4bb1684c2f2451` |
| CPU worker 镜像 | `d1635cdcb963447f78b9d67f8a146f5997b7f6db941f14f285ec2d14ea3e0aea` |
| GPU worker 镜像 | `370773d83713d4eeaea215c43cd58e34528bb9a94d7278f03c9febc6d44ba5fd` |
| GPU 任务镜像 | `95997fcca98f1a82baf748ce4d72d1e86f0c68af3080e656654a1631df5f7fa9` |

导入后九项真实 PostgreSQL/Compose 检查通过，见 `test-results/px-container-worker-727617a0e453/result.json`：最小权限、容器重建、显式关闭、无参数默认关闭、公开配置权限拒绝、额外数据库列权限拒绝、源码改写拒绝、恢复原源码和旧控制契约拒绝。实际 image ID、入口和空默认命令均核对；默认启动没有进入设备初始化。原锁 inode、journal 安装身份和数据库无 GPU/提交记录检查通过。

本轮没有查询 NVIDIA、映射设备或初始化 CUDA。该验收的十个容器、两个网络、数据库卷及临时私有配置已清理；导入的发布镜像保留供后续 CPU 验证，不将镜像保留误报为清理失败。

首轮实际发布结果为 `test-results/g42-release-first/release-acceptance.json`：129 个新实例 WASI 探针、非 root/只读根/无宿主挂载边界、浏览器实际 Run/错误提交/正确提交/私有记录与删除、三组语言/项目/本地 CLI 流程、七组扩展流程，以及实际排空超时关闭、继续处理、恢复和错误解释器拒绝均通过。扩展流程覆盖 CPU 专业题型、博客、题目反馈、组织、关闭模型时的辅导、挑战和类/API；没有运行模型或验证 GPU。该轮容器、网络及数据库卷清理通过。

## 第二轮制品

第二轮发布目录为 `var/releases/2026-11-01-g42-candidate-2`，构建及完整归档核验通过；原目录保留。构建记录为 `test-results/g42-build-final.log`、`release-build-ba55da7a085b.log` 及同名 `.gpu/` 子日志。第二轮 CPU/浏览器复验的全部产品流程和清理通过，报告为 `g42-release-final/release-acceptance.json`；输出路径仍有上述三个文件遗漏，记录在 `g42-browser-output-second.json`。此目录名对应的是部分输出路径修正，完整修正另用新的第三份制品。

| 身份 | SHA-256 |
| --- | --- |
| 发布清单 | `35835205ee764421dc5947f7a5ac463d3e8fc9a26d885b879328487e7bec8444` |
| 839 文件冻结源码 | `d79a0847543b399c389e7790d67215b3f824eb94c77f45a5cc03361be39f9b10` |
| Web 镜像 | `bbe06a318f1f21a24f291f38d0c1b199cd666e9360ece24a2f0b364eb1b1c357` |
| CPU worker 镜像 | `7403aa6f853b4874a666d056152b2043f5c08925ff0488cae012e42e44826116` |

两份包的 GPU worker、任务 image ID 及完整 GPU 元数据相同；前述九项无设备应用结果绑定首轮导入的这些确切身份，不将它们记为第二轮重复执行。输出目录修正不涉及应用、评测策略或 GPU 源码。

## 最终制品与 Git 对应

最终目录为 `var/releases/2026-11-01-g42-candidate-3`。构建与归档核验通过，记录为 `test-results/g42-build-complete.log`、`release-build-89ecc7fa8b6e.log` 及同名 `.gpu/` 子日志。最终浏览器/CPU 复验全部通过，报告为 `test-results/g42-release-complete/release-acceptance.json`：129 个真实探针、主流程、三组功能浏览器、七组扩展浏览器、排空/恢复、错误解释器拒绝和自有资源清理均通过。没有运行 GPU 或模型。

| 身份 | SHA-256 |
| --- | --- |
| 发布清单 | `44b6ca03b1e22d92382e5a04b578dee64813f57eceb1b3c3485117b0fdfa2142` |
| 冻结源码 | `7ebd4c107d7f7df38b05596e32baf5d5a39b70b2d5ac0d9b91a26f97d055d587` |
| Web 镜像 | `1c642b14ae6b00d8daa7ba95115d69749535a6dd26e79d9334faf5de5f01e158` |
| CPU worker 镜像 | `e1d4fe91926c1c41e8ed3a2de8106f9572f17b32c293ad2b0b2f030a3356b144` |

`test-results/g42-source-commit.json` 逐一读取 Git blob 并比较 SHA-256，确认全部 **839 个源码文件**与实现提交 `3cecc5f9a1c15be1748e65d42f28910b15af6a5f` 一致。GPU worker、任务镜像及完整元数据与前两轮相同；最终验收结论另以文档提交补齐，不改写冻结包。

## 最终复核与清理

`test-results/g42-audit.json` 的独立只读复核通过：最终发布包的完整归档、导入镜像身份、各轮报告和 127 项组件记录均符合；九项无设备 worker 检查对应的十个容器已不存在，三轮 CPU 发布容器/网络/数据库卷均已清理，三组构建的六个临时 GPU 标签已移除。导入的四个发布镜像保留，制品与日志也继续保留。

最终七个截图/子报告全部位于指定目录；第二轮结束后保存的公共记录，其 SHA-256 和修改时间均未变化。复用已有输出目录的实际 CLI 在 Docker 操作前以 `FileExistsError` 拒绝，原验收报告保持原字节和修改时间，日志为 `g42-output-directory-guard.log`。

原有 `praximesh-web-1`、`praximesh-worker-1`、`praximesh-tutor-1` 均运行且健康，没有重建。代码格式检查和本批文档链接检查通过；最终差异清单 `g42-final-source-audit.json` 确认后续补充只涉及五份交付文档，运行代码与测试均与最终冻结包保持一致。

## 剩余范围

构建与导入不激活站点、不签发 GPU 凭证。容器升级协调、完整 CUDA 启动与真实队列、总时间预算、候选内容/性能以及本地 GPU 工具继续交付；公开运营、许可证、目标主机和托管 CI 门槛仍按需求追踪保留。
