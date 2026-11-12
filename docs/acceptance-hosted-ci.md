# GitHub 托管应用与 WASI 验收

基线执行日期：2026-11-02。记录更新：2026-11-12。对应 PRD R-09、AC-07。

维护者授权后，`main` 的 111 个本地提交于 2026-11-02 推送到 `origin/main`。以下基线验收绑定提交 `2f2c9b0f95ead9f0c02646217c6af53d813a1f9c`；两项工作流均由该次 push 触发，并在首次运行中成功结束。后续结果在本页按各自提交分别记录。

| 工作流 | 结果 | 实际覆盖范围 |
| --- | --- | --- |
| [应用与内容检查](https://github.com/laf070810/PraxiMesh/actions/runs/41413486972) | `success` | SQLite、PostgreSQL、编辑器构建、浏览器和容器五项 job；容器 job 包含默认 Compose 验收 |
| [真实 WASI 验收](https://github.com/laf070810/PraxiMesh/actions/runs/41413486898) | `success` | 固定解释器构建、真实启动探针与评测测试、HTTPS/浏览器/数据库权限和容量、发布包构建与导入、迁移失败及中断恢复、依赖审计、制品上传 |

应用工作流于 2026-11-02 08:42 UTC 完成，WASI 工作流于 09:29 UTC 完成。2026-11-11 的 GitHub API 复核确认，两项工作流的 `head_sha`、`run_attempt=1`、终态及 WASI 全部步骤仍与原记录一致。工作流成功不将其内部按条件跳过的用例自动计作通过；本记录不借用其他版本或本地运行的用例数量。

## 已关闭的验收缺口

提交 `2f2c9b0` 的应用与 CPU/WASI 托管验收已完成，发布包导入步骤也已实际成功。旧提交 `d2c6560` 的[失败工作流](https://github.com/laf070810/PraxiMesh/actions/runs/41002028143)保留为历史记录；没有取得其完整日志，仍不能认定它与 [G61](acceptance-g61.md) 的本地浏览器问题同因。

## 制品与部署范围

`immutable-release` 按工作流配置保留七天。2026-11-11 查询时，它已不在该次运行的制品列表中；`real-wasi-acceptance` 报告仍列出。历史成功记录不能替代实际部署所需的完整发布包和独立确认的 manifest 摘要；需要部署时，应准备仍可取得且完成验证的制品，按[发布指南](deployment/releases.md)操作。

这些工作流运行在一次性 GitHub 托管节点。专用部署 runner 的访问限制、目标站点发布与恢复、长期运维、旧 F 应用到当前版本的整站升级，以及真实 GPU 全路径仍需分别验收。GPU、目标站点和维护者外部事项继续见[需求追踪](requirements.md)。本次结果不表示完整 PRD 已完成。

<a id="later-runs"></a>

## 后续提交与故障定位

2026-11-11 至 13 日核对后续 push；以下结果独立于 `2f2c9b0` 的成功记录。

| 提交 | 应用工作流 | WASI 工作流 |
| --- | --- | --- |
| `853b526` | [43031396429](https://github.com/laf070810/PraxiMesh/actions/runs/43031396429)：SQLite 测试失败，PostgreSQL 被取消；编辑器、浏览器、容器通过 | [43031396286](https://github.com/laf070810/PraxiMesh/actions/runs/43031396286)：成功 |
| `fc59265` | [43043838329](https://github.com/laf070810/PraxiMesh/actions/runs/43043838329)：SQLite 测试和 Chromium 安装失败，PostgreSQL 被取消；最后一次可用查询时容器 job 尚在运行 | [43043837878](https://github.com/laf070810/PraxiMesh/actions/runs/43043837878)：Chromium 安装失败 |
| `10242e5` | [43058131760](https://github.com/laf070810/PraxiMesh/actions/runs/43058131760)：SQLite、PostgreSQL、编辑器、容器通过；浏览器系统依赖安装失败 | [43058132599](https://github.com/laf070810/PraxiMesh/actions/runs/43058132599)：系统依赖安装失败 |
| `369a881` | [43058725539](https://github.com/laf070810/PraxiMesh/actions/runs/43058725539)：SQLite、PostgreSQL、编辑器、容器通过；浏览器系统依赖安装失败 | [43058725571](https://github.com/laf070810/PraxiMesh/actions/runs/43058725571)：系统依赖安装失败 |
| `f89a480` | [43063191247](https://github.com/laf070810/PraxiMesh/actions/runs/43063191247)：PostgreSQL、编辑器、浏览器、容器通过；SQLite 的一个测试函数失败，已取得位置注释 | [43063191233](https://github.com/laf070810/PraxiMesh/actions/runs/43063191233)：成功，包含真实评测、HTTPS、发布包和迁移失败/中断恢复 |
| `a5d96c7` | [43071071772](https://github.com/laf070810/PraxiMesh/actions/runs/43071071772)：两种数据库、编辑器、浏览器和容器全部通过 | [43071071748](https://github.com/laf070810/PraxiMesh/actions/runs/43071071748)：成功，包含真实评测、HTTPS、发布包和迁移失败/中断恢复 |
| `3cdfff2` | [43076789097](https://github.com/laf070810/PraxiMesh/actions/runs/43076789097)：全部通过；此提交只更新文档 | [43076788801](https://github.com/laf070810/PraxiMesh/actions/runs/43076788801)：成功；本地同源冻结包也已通过独立验收 |
| `95b24e4` | [43084734133](https://github.com/laf070810/PraxiMesh/actions/runs/43084734133)：两种数据库、编辑器、浏览器和容器全部通过 | [43084734130](https://github.com/laf070810/PraxiMesh/actions/runs/43084734130)：成功 |
| `fa58c7a` | [43261015099](https://github.com/laf070810/PraxiMesh/actions/runs/43261015099)：11 月 12 日 16:53 UTC 观察时仍在运行 | [43261015054](https://github.com/laf070810/PraxiMesh/actions/runs/43261015054)：同次观察时仍在运行；本地无设备镜像检查另行记录 |

早期失败注释只有退出码；日志接口返回 403，后续匿名 API 查询也遇到限流。没有取得完整失败文本，不能确认早期 SQLite 或安装失败的原因。Node 弃用警告不作为失败原因。

从 `git archive 853b526` 创建独立源码和环境，使用冻结依赖、Python 3.11.15 与 uv 0.11.8，本地 SQLite 全套为 **4023 passed / 195 skipped**，未复现远端错误；临时环境已经删除。宿主与 GitHub Ubuntu runner 不同，跳过项也未计作通过。相关记录为 `sqlite-reproduction.json`、`sqlite-reproduction.xml`；主服务虚拟环境没有同步依赖。

提交 `10242e5` 增加只公布固定提交测试位置的失败摘要，保留原失败；两种数据库取消联动，并拆分系统依赖安装与浏览器下载。诊断模块的十七项检查通过，覆盖真实 Git 对象、替换对象和私有参数边界；工作流 YAML、内嵌命令及失败条件通过校验。对应远端结果已列在上表。当前 CPU 包的本地实际评测、浏览器等待调整及范围另见[升级准备](acceptance-upgrade-preparation.md)。

11 月 12 日进一步核对确认，两个后续提交的两种数据库均已通过；这不能还原此前 SQLite 失败的具体原因。安装失败发生在 `install-deps chromium`，尚未执行浏览器下载、截图或 WASI 验收。锁定 Playwright 的该入口依次调用 `apt-get update` 与安装，公开注释仍只有退出码，尚不能确定具体 APT 错误。

本轮增加内存中的安装错误分类，保留原命令、job 输出和退出状态。原始日志制品方案因可能外发内部地址等元数据被自动审批拒绝，已改为不生成日志文件、只输出固定类别。十一项新检查与原十七项诊断检查共 **28 passed**，包括实际固定子进程的正常/非零/信号退出、无新增文件及诊断容量边界。没有在开发主机运行 APT；这些结果不证明远端安装故障已经解决。分类规则和运行方式见 [CI 指南](deployment/ci.md)。

<a id="fractional-clock"></a>

## SQLite 失败位置与分数时钟复现

`f89a480` 的安装步骤和浏览器完整检查已通过，安装调用及版本没有改变，不能据此确定此前 APT 失败的原因。其 SQLite job 的真实注释定位到 `tests/test_container_practice.py::test_late_startup_change_prevents_sending_learner_source`；参数值和异常正文没有公开。

在原本地环境直接重放该函数，五种状态变化均通过。将冻结时钟固定为 `934.0000000000003` 后，五种变化均在初始恢复的夹具预算断言处失败：构造的期限为 `1114.0000000000005`，相减得到 `180.0000000000001`，误判超过 180 秒。这发生在预期的启动后身份/取消检查之前。

仅修正夹具后，相关三个模块为 99 passed / 5 failed，分数时钟继续在生产适配器的维护预算判断处被拒绝。进一步独立验证同类故障预算和完整启动总预算；完整启动使用 `3872.0000000000027` 构造 4320 秒期限时，相减得到 `4320.000000000001`，也会被误拒绝。

三个生产入口及对应夹具改为比较原绝对期限与 `now + 原预算`，保持原 90/180/4320 秒常量、严格最小预算、类型及有限值检查，不加容差或刷新期限。回归覆盖取消、环境、私有标记、操作记录和期限变化，并断言完整启动确实已走到最后阶段、源码仍未发送；紧邻上界的下一个浮点值继续被拒绝。独立启动夹具也补齐了真实任务构造器已有的清理状态字段，避免模型异常掩盖原失败。

三个文件已在执行源码契约中，故障模块也在运行时源码契约中；本轮会改变环境/镜像摘要，需要匹配制品重新验收。当前没有取得远端异常正文，因此本地确定性复现不能证明所有历史 SQLite 失败都同因。预算决定见 [RFC-021](rfcs/021-gpu-container-supervision.md#监督契约)及 [ADR-055](adr/055-container-controller-faults.md)。

修复已提交并推送为 `a5d96c740355e20933f979626f5aef934c11cf53`。六个相关模块 **225 passed / 0 skipped**，包括新增数值边界、全部启动后变化、独立恢复拒绝、真实私有文件/锁和进程中断；结果为 `deadline-components-final.xml`。源码契约已按实际文件摘要核对。

该提交随后完成以下本地实际检查，按资源预算串行运行：

- 自有 PostgreSQL 的启动、凭证、就绪竞争、准入与预约恢复：**128 passed / 0 skipped**。
- 重建匹配运行时的五组 CPU/gVisor 故障、准备/恢复失败和新正确调用：**全部通过，十五个原容器实例完成清理**；独立超时仍在主动清理前观察退出。
- 当前 worker 镜像、Compose 与 PostgreSQL 只读预检：**十项通过，十一个自有容器及相关网络、卷、临时存储清理完成**。没有查询、映射或执行 GPU。
- 原 Web、CPU worker 和 tutor 的容器 ID、镜像、启动时间均未变，健康状态正常。

这些检查的汇总在 `test-results/deadline-bounds-2026-11-12/`，包括 `postgres-summary.json`、`lifecycle-process.json`、`worker-confirmation.json` 和 `final-resource-audit.json`；原始实际报告分别为 `test-results/px-container-lifecycle-cb2a8e188ab8/result.json` 与 `test-results/px-container-worker-e1b55b4536db/result.json`。测试镜像标签已经清理，不作为现成部署制品交付；正式使用须按新源码构建匹配镜像并完成真实硬件启动。

`a5d96c7` 的应用五项 job 与完整 WASI 工作流均已通过。WASI 于 2026-11-11 18:35 UTC 成功结束，23 个步骤成功，失败位置摘要按条件跳过；工作流内的测试跳过项仍不计作通过。最终步骤复核在 `final-wasi-verification.json`。此前安装失败和旧 SQLite 失败的完整异常仍未取得，本次成功不将历史原因全部归结为同一问题。随后从仅更新文档的 `3cdfff2` 构建并实测了[新的冻结 CPU 目标包](acceptance-upgrade-preparation.md#current-target)，供旧 F 整站升级获许可后使用。

原始证据包括 `f89a480-sqlite-annotations.json`、`late-startup-original.xml`、`late-startup-fractional-before.xml`、`late-startup-regression-before.xml`、`late-startup-components-after.xml`、`production-deadline-boundaries-before.xml`、`startup-total-boundary-before.xml` 和 `deadline-source-contracts.json`，均位于下述忽略目录。这些检查使用真实临时文件/锁与明确的设备模型，没有访问 GPU。

## 原始证据

首次推送和运行期间的公开 API、运行页面、job 页面及最终步骤记录保存在忽略目录 `test-results/github-main-2f2c9b0/`。后续复核与诊断在 `test-results/hosted-ci-2026-11-11/`，包含工作流、job、制品列表、本地复现和查询摘要。对 `2f2c9b0` 的复核没有重跑其工作流或部署站点。

2026-11-13 复核 `3cdfff2` 与 `95b24e4` 的两项工作流均为成功终态；后者五个应用 job 与一个完整 WASI job 的公开状态保存在 `test-results/current-tool-images-2026-11-13/hosted-ci.json`。随后发现的镜像暂存权限问题及其独立验证见[镜像权限记录](acceptance-image-staging.md)；上述 CI 结果仍只对应各自提交。

`fa58c7a` 的初次 API 记录和随后公开 Actions 页面复核保存在同目录的 `implementation-ci-first.json` 与 `implementation-ci-final-observation.json`。中途匿名 API 返回限流 403，因此改读公开页面并按完整提交链接匹配。尚未取得该提交的成功终态，本地通过不代替托管结果。
