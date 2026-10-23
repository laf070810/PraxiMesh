# 重放生产设置的 HTTPS 验收

此验收在 Linux x86_64 上创建一次性的真实部署：Caddy → Gunicorn（2 个进程、各 2 个线程）→ PostgreSQL，另有 1 个真实 WASI worker。全部端口只监听回环地址，数据库和账号均为合成测试数据。它不修改系统证书、共享 Docker daemon 或任何已有服务。设计依据见 [RFC-003](rfcs/003-production-acceptance.md)。

## 准备与运行

需要 Docker、OpenSSL、Python 3.11、uv，以及 Chromium 所需系统库。Docker 只运行可信 PostgreSQL，不运行学习者代码。先按 [WASI 指南](wasi.md)在当前机器构建制品，并配置该次可信构建产生的 `JUDGE_BACKEND=wasi`、`JUDGE_WASI_BUNDLE` 和 `JUDGE_WASI_SHA256`。

```sh
uv sync --frozen --group judge --group browser
.venv/bin/playwright install chromium
.venv/bin/python scripts/fetch_caddy.py --output var/tools/caddy-2.11.7
.venv/bin/python scripts/production_acceptance.py --caddy var/tools/caddy-2.11.7
```

下载器只获取固定摘要的官方 Caddy 2.11.7，并保留其 LICENSE；目标已存在时拒绝覆盖。已下载的可信版本可直接重用。Docker 按固定 digest 获取 PostgreSQL 17。需要离线运行时先准备这些工具、浏览器、数据库镜像和 WASI 制品。使用 rootless Docker 时，由调用环境设置已有 daemon 的 `DOCKER_HOST`，脚本不会修改 daemon 或登录配置。

## 验收内容

- owner 仅用于迁移、静态资源收集、内容导入和测试账号准备；Web 与 worker 分别使用受限角色。检查 Web 不可创建表、worker 不可读取账号表。
- `PRAXIMESH_MODE=production`、真实 `check --deploy --fail-level WARNING`、关闭 DEBUG；独立临时静态目录。
- 临时私有 CA 签名的 HTTPS：Python 客户端验证证书链和主机名，拒绝错误 CA/主机名；HTTP 重定向、HSTS、CSP、请求体上限、404 页面和 Host 检查。
- 实际代理覆盖伪造的协议/IP 头；来自非信任连接地址的 HTTPS 头不能绕过后端重定向；Gunicorn 不从其他代理头独立推断 HTTPS。
- 123 项真实 WASI 探针，然后经 HTTPS 完成注册、Secure cookie、私有草稿、Run、错误 Submit、Accepted、路径/历史、移动页面与删除账号。
- 8 个并发客户端各阶段共 120 次页面请求；6 个账号同时提交正确函数，用真实结果记录排队、执行和总等待分位数，并在判题期间再次测浏览。
- 停止本次 worker 后等待真实心跳失效，提交返回 503 且不入队；阅读和草稿继续可用。伪造不同 X-Real-IP 不能绕过真实来源 IP 的账号限流。

浏览器只对本次临时服务器公钥的 SPKI 设置证书例外，脚本限制为 HTTPS 回环 URL；不全局忽略证书错误，不安装系统 CA。证书链与主机名由独立的 Python HTTPS 检查验证；错误 CA 必须产生证书验证错误，未知 SNI 也可在 Caddy 选择证书前直接拒绝握手，摘要分别记录异常类型。数据库 TLS 在这个临时回环测试中关闭；生产远程数据库仍须使用 `verify-full` 并另行验收。

## 输出与限制

成功时输出 `test-results/production-acceptance.json`，以及服务日志、探针记录、浏览器截图。JSON 记录样本量、并发、分位数算法、真实制品摘要和全部检查结果，不包含密码、私钥或学习者源码。任何检查失败都会使命令非零退出；已启动的服务、临时数据库、凭证和证书仍会清理。新运行开始会删除旧的成功摘要，避免把旧结果误当成当前通过。

开发回归目标是本地浏览 p95 < 1 秒、6 个账号的一次提交突发在 60 秒内全部结束。每道题的测试数量、CPU/磁盘、数据库网络和恶意超时提交都会影响容量；这些小样本不能证明持续峰值、公网 SLO 或全局 100 个排队任务的可接受等待时间。默认每用户最多 2 个待处理任务，队列上限和超时策略见[运维手册](operations.md)。

`.github/workflows/wasi.yml` 在临时托管 runner 构建 WASI 后执行同一部署验收。工作流定义和本地执行不能代替托管 CI 的成功记录。公有域名、ACME 签发/续期、远程数据库 TLS、目标主机 systemd/cgroup、独立安全复核与运营政策仍属于生产发布验收。
