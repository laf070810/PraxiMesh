# G58：真实 Web 端口恢复与中断续跑

日期：2026-11-02。对应 PRD R-09、AC-07 和 [RFC-023](rfcs/023-web-container-recovery.md)。[G44](acceptance-g44.md) 的六项真实 Docker/HTTP 专项全部通过；重放入口统一在[部署恢复指南](deployment/production.md#web-recovery-check)。本轮没有改变应用或恢复实现。

## 镜像与执行范围

使用本账号 rootless Docker 中已有的 Compose Web 镜像 `sha256:90633fed114508a841dabfd6d55ff572c7b8309b4aedca78b92cb517d71bc734`。其用户为 `65532:65532`，无入口脚本、无声明卷。探针从镜像创建从未启动的临时容器，仅派生固定的 Python HTTP 命令；未修改或提交运行中的服务容器。

没有导入旧 F 或 G42 镜像，没有更改 daemon 或访问 GPU。先前旧缓存恢复的审批限制未被重试。探针使用真实 Docker 容器、回环端口、HTTP 和进程中断，协调器/代理元数据则是合成夹具；没有数据库、Caddy、实际应用流量、学员代码或 WASI。

## 实际结果

`test-results/g58-web-container/result.json` 和 `g58-web-container.log` 保留完整结果，六项全部通过：

| 检查 | 实际观察 |
| --- | --- |
| 原端口仍被占用 | 原端口 35315 的启动产生 RootlessKit 诊断；占用保持时，持久重建分配新端口 32840，实际 HTTP 200 |
| 移除旧容器后中断 | 子进程 SIGKILL，退出码 -9；日志仍为 prepared，继续后 HTTP 200 |
| 创建新容器但未保存 ID 时中断 | 退出码 -9；日志仍为 removed，核对原意图后继续，HTTP 200 |
| 保存新 ID 后中断 | 退出码 -9；日志为 created，继续后 HTTP 200 |
| 启动成功但未确认时中断 | 退出码 -9；日志为 created，继续原对象后 HTTP 200 |
| 同名容器替换 | 拒绝按名称停止替代对象；该对象保留到按自有测试身份清理 |

原始失败诊断为 `control-failure-8d35c35e1cc942d68bde16b93da7b3bc.json`，保存在受限结果目录。结果还记录 `scripts/deploy.py`、`scripts/deploy_web.py`、`scripts/deploy_web_acceptance.py` 的 SHA-256；执行源码来自 `3d9d3a4`，本轮只更新文档。

## 清理与剩余范围

`test-results/g58-resource-check.json` 独立确认测试标签下无遗留容器、网络或派生镜像，原基镜像仍在。已有 Web、worker、tutor 的完整容器 ID、镜像 ID 和启动时间与运行前一致。清理使用 `--no-prune`；没有重复 G44 的父镜像缓存删除。

这补齐了端口恢复专项，不能替代实际 PostgreSQL、HTTPS、WASI、四代切换、曝光前后中断、用户写入/删除保留与 worker 自动重启矩阵。数据库组件的 77 项真实检查见 [G57](acceptance-g57.md)，两者均不证明整站或 GPU 部署完成。

后续 [G59](acceptance-g59.md) 已使用新建的当前 CPU 制品通过上述完整发布恢复矩阵，包含额外的真实失败迁移；旧 F→当前 schema 升级与 GPU 验收仍分别待完成。本轮固定 HTTP 专项的范围保持不变。
