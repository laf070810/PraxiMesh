# RFC-003：HTTPS 部署验证与发布证据

状态：接受，隔离部署实测通过。日期：2026-10-23。关联：PRD R-09 / AC-05 / AC-07。决策见 [ADR-005](../adr/005-production-acceptance.md)，重放见[部署验收指南](../production-acceptance.md)。

本地开发服务器的成功不能证明生产设置、代理、TLS 或数据库角色可用。本轮建立一次性的真实部署验收：Caddy TLS → Gunicorn（生产设置）→ PostgreSQL，并接入实际 WASI worker。所有端口只绑定回环地址；不安装系统证书、不使用公开域名、不改共享 Docker 配置。

## 边界与选择

生产代理继续采用标准 Caddy。应用只接受显式可信连接地址提供的单值 X-Forwarded-Proto 和有效单值 X-Real-IP；最前面的中间件在 Django 的 HTTPS/CSRF 判断前清理不可信协议头。Gunicorn 不再独立从其他代理头推断 scheme，避免两套信任规则不一致。后端仍必须限制为代理可访问，不能靠请求头当作网络认证。

验收使用临时私有 CA 签发的服务器证书。Python HTTPS 客户端必须验证证书链、主机名、TLS 协议和错误 CA 拒绝；浏览器仅为本次测试的 SPKI 公钥设置例外，禁止全局忽略证书错误。此浏览器例外限回环 URL，与私有 CA 校验分开记录，不替代生产公有证书、DNS 或续期测试。

PostgreSQL 在独立临时容器内；迁移/导入用临时 owner，Web 用业务表白名单角色（内容与健康记录只读），worker 用已约定的题目/任务/健康记录白名单。实测 worker 不能读账号，Web 不能创建 schema 对象或修改题目/健康记录。TLS、Cookie/CSRF、代理头、生产错误页、真实判题和容量检查均不连接用户生产库。

## 性能与发布

对同一真实部署记录并发浏览的延迟分位数，以及多个账号真实提交的排队/执行/总等待时间。记录样本量、并发数、进程数和环境，给出小规模教学实例的开发回归目标；不将本地样本外推为公网 SLO。耗尽配额、worker 失联时仍须有准确反馈。

验收脚本已进入 WASI CI 定义，实际本地运行输出不含凭证/源码的摘要。没有仓库 remote、registry、域名或部署主机时，继续实现本地可审查的自动化，不能宣称托管 CI/CD 或公开部署已完成。当前数字与边界见[验收记录](../acceptance.md)。

## 官方依据

- [Django 5.2 代理 HTTPS 配置](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header)。
- [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls)、[反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[请求体限制](https://caddyserver.com/docs/caddyfile/directives/request_body)。
- [Chromium 的 SPKI 例外定义](https://chromium.googlesource.com/chromium/src/+/main/services/network/public/cpp/network_switches.cc)。例外仅供隔离测试浏览器，使用正常证书的部署不需要该设置。
