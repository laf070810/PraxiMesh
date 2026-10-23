# 本地练习与提交

API 与 CLI 的 B2 联合运行时、镜像及 HTTPS 验收见 [B2 验收](acceptance-b2.md)；后续专业题型见 [C 验收](acceptance-c.md)。类/API 扩展的实际CLI、HTTPS及固定镜像结果见 [E 验收](acceptance-e.md)。

本地公开样例帮助调试，不增加服务端完成记录，也不能上传本地分数。正式提交由平台按题目固定版本、受保护测试与当前可用评测环境重新执行。

## 创建令牌

登录后在“账号 → 本地练习访问令牌”输入当前密码，选择有效期和权限。令牌有效期为 1–90 天，每个账号最多十个有效令牌，原文只显示一次。两个可选权限分别为“提交练习”和“读取自己的结果”，不包含修改账号、公开内容或读取别人数据。可随时撤销；账号恢复会撤销全部令牌，删除账号会删除相关记录。

在自己的终端用不回显输入设置环境变量，不把令牌放进命令参数、代码、练习文件或版本控制：

```sh
# Zsh
read -rs 'PRAXIMESH_API_TOKEN?API token: '
export PRAXIMESH_API_TOKEN
```

上面的提示语语法适用于 Zsh；Bash 可用 `read -rs -p 'API token: ' PRAXIMESH_API_TOKEN`。

## 下载与编辑

在仓库根目录执行。网络命令只需要 Python 标准库。

```sh
python scripts/practice.py --server https://your-instance.example download sum-positive ./sum-positive-work
```

目标目录必须不存在，父目录必须存在。工具创建私有目录、`exercise.json` 和对应语言的解答文件。JSON 含公开题意、公开测试、固定版本身份及来源服务器；不含参考实现和受保护用例。编辑 `solution.py`（JavaScript 题为 `solution.js`），不要改变版本元数据。

类/API 题（例如 `bounded-queue-api`）也使用 `solution.py`，实现题目声明的类和方法。公开包含构造与方法调用序列；同一场景保留状态，场景之间重置。无需编写 `solve` 函数或宿主测试脚本。语义与异常规则见[类/API 契约](object-api-contract.md)。

## 在本地沙箱运行公开样例

先按[开发指南](../README.md)安装锁定的 judge 依赖，并取得维护者按[WASI 构建说明](wasi.md)构建和校验的 WASI bundle。下面的路径与摘要应替换为实际发布值。

```sh
.venv/bin/python scripts/practice.py test ./sum-positive-work \
  --bundle /absolute/path/to/verified-bundle \
  --bundle-sha256 MANIFEST_SHA256
```

工具检查 bundle、解释器依赖与沙箱政策，随后在 WASI 内运行学员程序，不执行宿主 Python/Node 或下载的构建脚本。结果带 `formal_result: false`、bundle 和政策摘要。成功退出码为 0，公开测试未通过为 1，配置或传输错误为 2。此功能要求受支持的非 root Linux x86_64 环境；不可用时仍可编辑并提交服务器。

## 正式提交与查询

```sh
python scripts/practice.py --server https://your-instance.example submit ./sum-positive-work
python scripts/practice.py --server https://your-instance.example status SUBMISSION_UUID
```

提交前会打印一个请求编号。连接中断后无法仅凭客户端判断服务器是否收到请求；用 `--request-key REQUEST_UUID` 重试相同内容，服务器返回同一任务，不重复入队。改变源码、题目或模式后需要新编号。`--mode run` 只在服务器运行公开样例，不计正式完成。题目当前版本已变化时服务器返回 409，需下载新版本到新目录再迁移自己的代码。

提交命令必须显式给出服务器，且须与练习的下载来源一致。远程连接要求 HTTPS；可用 `--ca-file` 指定受信任 CA，不能跳过证书验证。`--allow-local-http` 仅供显式指定 localhost、127.0.0.1 或 ::1 的开发实例使用。客户端拒绝重定向，不使用环境中的 HTTP 代理，不在错误消息中输出响应正文或凭证。

## API v1

| 方法与路径 | 认证与内容 |
| --- | --- |
| `GET /api/v1/problems/` | 公开题目列表，每页 50 条，`?page=N` |
| `GET /api/v1/problems/ID/` | 当前版本的公开任务包 |
| `POST /api/v1/problems/ID/submissions/` | Bearer 提交权限、`Idempotency-Key` UUID、JSON `{version_id, source, mode}` |
| `GET /api/v1/submissions/UUID/` | Bearer 本人结果读取权限；不返回源代码 |

提交不接受客户端分数、测试结果、用户 ID 或环境指纹。新任务经过与网页相同的队列、版本和待处理额度检查。每账号每分钟最多 60 个已认证 API 请求，每来源地址每分钟最多 120 个认证尝试，正式入队另受现有每分钟 12 次提交额度和待处理任务上限约束。幂等重放不重复占用提交额度。浏览器 Cookie 不能单独授权这些接口，Bearer 令牌也不能替代网页的会话与 CSRF。

`formal_result` 仅表示服务器已完成一个有判定结果的正式提交，不等于 Accepted。服务故障、排队、运行中和 Run 均不是有效正式结果。接口返回的通过状态对应真实测试，公开成绩仍需要另行主动分享。
