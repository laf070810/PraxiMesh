# gVisor 执行后端参考（尚未完成本机验收）

[部署方案选择](README.md) · 本页供隔离运行时开发与目标节点验证，不属于已验收的安装方案。当前 Compose 和发布协调器使用 WASI；gVisor 适配器只支持 Python JSON 函数，也不提供 GPU 题型。


执行节点必须是专用、可销毁的 Linux 节点，不承载其他用户业务；运行时维护者参考 [gVisor 官方安装说明](https://gvisor.dev/docs/user_guide/install/) 安装受支持版本，并按 [Docker 集成文档](https://gvisor.dev/docs/user_guide/quick_start/docker/) 注册 `runsc`。本项目不自动修改宿主 Docker 配置或重启共享 daemon。记录 runsc 二进制校验、版本、Docker/内核版本、CPU 型号和 cgroup 配置，检查资源限制可用。

预检要求 Docker `info` 的 `MemoryLimit`、`SwapLimit`、`PidsLimit` 和 `CpuCfsQuota` 均为布尔值 `true`。Docker 的字段拼写是 `CpuCfsQuota`；缺失、关闭或非布尔值均拒绝，不能用其他拼写、普通容器或忽略 CPU 限额来绕过。检测到 NVIDIA 设备也不等于已经验证 GPU 执行隔离；gVisor 的 GPU 支持还涉及特定驱动和独立验收，见[官方 GPU 边界说明](https://gvisor.dev/docs/user_guide/gpu/)。

初始 judge 镜像为干净的官方 Python 3.11 image（可使用 `deploy/images.env` 的 digest），**不能使用包含数据库配置/项目源码的 Web 镜像**。guest 只看到镜像、受限临时目录与当前测试输入。预期答案保留在 worker。

在执行节点的独立环境安装同版本依赖及 Docker CLI。数据库使用受限 worker 角色，按当前 [deploy_database.py](../../scripts/deploy_database.py) 中的权限白名单配置并实际复验；旧阶段的几条 GRANT 不足以覆盖当前队列锁与资源预约。不要授予账号、会话、草稿或迁移权限，凭证仅由可信控制器持有。

```sh
# 通过受保护的环境文件配置 DATABASE_URL、生产设置以及下列参数。
export JUDGE_BACKEND=gvisor
export JUDGE_IMAGE='python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15'
export JUDGE_RECEIPT='/srv/praximesh/var/sandbox-receipt.json'
docker pull "$JUDGE_IMAGE"
# worker 停止时进行检查，避免把其他正在运行的评测误报为遗留容器。
.venv/bin/python manage.py sandbox_check
RUN_SANDBOX_TESTS=1 .venv/bin/pytest -m sandbox -q
# 参考解、错误变体、全部隔离探针、安全复核通过后再启用：
export JUDGE_ENABLED=1
.venv/bin/python manage.py judge_worker --worker-id judge-1
```

Web 配置同一 `JUDGE_IMAGE` 与 `JUDGE_ENABLED=1`。worker 需 24 小时内的匹配检查记录；过期或任何执行/清理异常会停止接收工作。工作节点启动配置示例见 `deploy/praximesh-worker.service.example`。重新运行检查前先停 worker，不伪造/手改 receipt。修改判题策略或镜像后必须重测。

完整浏览器判题验收：在测试实例和专用 worker 都可用时设置 `BROWSER_JUDGE=1`，运行 `tests/browser_smoke.py`。它必须分别收到真实正确解通过和错误解失败，不能注入假状态。该流程已在 WASI 后端实际通过；gVisor 因本机无 runsc 仍未执行。可用 `scripts/browser_acceptance.py --judge` 创建并清理隔离测试实例。
