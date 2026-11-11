# GitHub 托管应用与 WASI 验收

执行日期：2026-11-02。复核日期：2026-11-11。对应 PRD R-09、AC-07。

维护者授权后，`main` 的 111 个本地提交已推送到 `origin/main`。本次验收绑定提交 `2f2c9b0f95ead9f0c02646217c6af53d813a1f9c`；两项工作流均由该次 push 触发，并在首次运行中成功结束。

| 工作流 | 结果 | 实际覆盖范围 |
| --- | --- | --- |
| [应用与内容检查](https://github.com/laf070810/PraxiMesh/actions/runs/41413486972) | `success` | SQLite、PostgreSQL、编辑器构建、浏览器和容器五项 job；容器 job 包含默认 Compose 验收 |
| [真实 WASI 验收](https://github.com/laf070810/PraxiMesh/actions/runs/41413486898) | `success` | 固定解释器构建、真实启动探针与评测测试、HTTPS/浏览器/数据库权限和容量、发布包构建与导入、迁移失败及中断恢复、依赖审计、制品上传 |

应用工作流于 2026-11-02 08:42 UTC 完成，WASI 工作流于 09:29 UTC 完成。2026-11-11 的 GitHub API 复核确认，两项工作流的 `head_sha`、`run_attempt=1`、终态及 WASI 全部步骤仍与原记录一致。工作流成功不将其内部按条件跳过的用例自动计作通过；本记录不借用其他版本或本地运行的用例数量。

## 已关闭的验收缺口

当前提交的应用与 CPU/WASI 托管验收已完成，发布包导入步骤也已实际成功。旧提交 `d2c6560` 的[失败工作流](https://github.com/laf070810/PraxiMesh/actions/runs/41002028143)保留为历史记录；没有取得其完整日志，仍不能认定它与 [G61](acceptance-g61.md) 的本地浏览器问题同因。

## 制品与部署范围

`immutable-release` 按工作流配置保留七天。2026-11-11 查询时，它已不在该次运行的制品列表中；`real-wasi-acceptance` 报告仍列出。历史成功记录不能替代实际部署所需的完整发布包和独立确认的 manifest 摘要；需要部署时，应准备仍可取得且完成验证的制品，按[发布指南](deployment/releases.md)操作。

这些工作流运行在一次性 GitHub 托管节点。专用部署 runner 的访问限制、目标站点发布与恢复、长期运维、旧 F 应用到当前版本的整站升级，以及真实 GPU 全路径仍需分别验收。GPU、目标站点和维护者外部事项继续见[需求追踪](requirements.md)。本次结果不表示完整 PRD 已完成。

## 原始证据

首次推送和运行期间的公开 API、运行页面、job 页面及最终步骤记录保存在忽略目录 `test-results/github-main-2f2c9b0/`。2026-11-11 的只读复核在 `test-results/hosted-ci-2026-11-11/`，包含工作流、job、制品列表及查询摘要。本轮复核没有重跑测试或部署站点。
