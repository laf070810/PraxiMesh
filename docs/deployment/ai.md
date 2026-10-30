# 可选的自部署 AI 辅导

[部署方案选择](README.md) · AI 是部署方案的可选能力。本页说明已有 HTTPS 服务配置、接口限制及模型验证。

学习目标、固定版本的练习计划、多维正式评测证据和作者分层提示不需要模型服务。AI 提示默认关闭。启用后，用户先预览将发送的公开题意、问题、主动选中的提示和可选源码，再确认发送；模型没有工具，也不能更改成绩。

## Compose 本地模型

可用 `docker compose -f compose.yaml -f compose.ai.yaml up --build -d` 同时启动 Web、真实评测和固定的 CPU 模型。无需宿主模型服务；模型在镜像构建时下载并校验，令牌自动持久生成。使用方法和数据边界见 [Compose 指南](compose.md)。

## 配置固定 HTTPS 服务

原生部署可设置下列环境变量。`COACHING_TOKEN` 使用该服务的专用凭证，只供 Web 进程使用；不要放入仓库、命令行或 worker 环境。

```dotenv
COACHING_ENABLED=1
COACHING_ENDPOINT=https://tutor.example.org/v1/chat/completions
COACHING_MODEL=operator-selected-model
COACHING_PROVIDER_DESCRIPTION=本组织维护的模型服务；请填写真实的数据处理与保留说明。
COACHING_DAILY_LIMIT=5
COACHING_CONCURRENCY=1
COACHING_MAX_TOKENS=192
COACHING_DISABLE_THINKING=0
```

接口使用固定的 Chat Completions JSON 结构，必须返回一条 `assistant` 文本和 `stop` 或 `length` 结束原因。无流式传输、工具调用、重定向或环境代理。请求硬截止 20 秒，响应最多 32 KiB，文本最多 4,000 字符。`length` 会明确标注截断，缺失 token 用量保持未知。服务失败不生成替代答案，不影响阅读、计划或正式判题。

HTTPS 必须验证证书。私有 CA 可使用 `COACHING_CA_FILE`，或在秘密配置中设置 `COACHING_CA_PEM` 为 PEM 文本，二者不能并用。不要关闭证书校验。端点、模型、提供方说明和凭证只能由维护者设置，用户无法指定地址或请求头。

使用发布协调器时，在**首次 `init` 前**将以下对象加入私密 `coordinator.json` 的 `application.coaching`。省略该对象即关闭 AI；主配置仍须由当前用户拥有且权限为 0600。协调器仅将这些设置传给 Web，不传给数据库管理容器或判题 worker。

```json
{
  "endpoint": "https://tutor.example.org/v1/chat/completions",
  "model": "operator-selected-model",
  "token": "REPLACE_WITH_DEDICATED_PROVIDER_TOKEN",
  "description": "本组织维护的模型服务；填写真实的数据处理与保留说明。",
  "daily_limit": 5,
  "concurrency": 1,
  "max_tokens": 192,
  "disable_thinking": false
}
```

私有证书可增加 `ca_pem` 字段，无需给 Web 挂载宿主目录。该端点必须能由 Web 容器解析和访问。协调器禁止明文端点；已有实例继续遵循配置摘要检查，不能直接改写配置后绕过协调器恢复检查。服务容量、模型质量和提供方数据政策应由部署者验证。

## 重放有界 CPU 模型检查

本地验证使用以下固定制品，模型权重不包含在平台发布包内。它们只证明真实推理链路可用，不构成默认模型推荐。

| 制品 | 固定来源与 SHA-256 |
| --- | --- |
| llama.cpp 源码 | [commit 620d2225cc436ab0b001e3200e590e3b286ef9ab](https://github.com/ggml-org/llama.cpp/tree/620d2225cc436ab0b001e3200e590e3b286ef9ab)；源码 tar.gz `1d4a987d9bd048f6e93107e838026438df70fbde237fc0c69c63d600512b4bd9` |
| Qwen3-0.6B-Q8_0.gguf | [官方固定模型版本](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/tree/23749fefcc72300e3a2ad315e1317431b06b590a)；`9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031`，639,446,688 字节 |

在独立目录下载并先核验摘要，按 [llama.cpp 构建说明](https://github.com/ggml-org/llama.cpp/blob/620d2225cc436ab0b001e3200e590e3b286ef9ab/docs/build.md) 准备 CMake/C++ 工具链。此处测试的是 Linux CPU 构建：

```sh
cmake -S /path/to/llama.cpp -B /path/to/build \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DGGML_NATIVE=OFF -DGGML_CPU=ON -DGGML_CUDA=OFF \
  -DGGML_VULKAN=OFF -DGGML_METAL=OFF -DGGML_BLAS=OFF \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_APP=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
  -DLLAMA_BUILD_MTMD=OFF -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_SUBPROCESS=OFF -DLLAMA_OPENSSL=OFF
cmake --build /path/to/build --target llama-server --parallel 2
sha256sum /path/to/build/bin/llama-server
```

记录自己构建的二进制摘要，再运行仓库中的有界启动器；CPU 编号需属于当前进程允许的 affinity，并避免与判题 worker 共用繁忙核心：

```sh
python scripts/local_coaching_server.py \
  --binary /path/to/build/bin/llama-server \
  --binary-sha256 YOUR_VERIFIED_BINARY_SHA256 \
  --model /path/to/Qwen3-0.6B-Q8_0.gguf \
  --model-sha256 9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031 \
  --state /tmp/praximesh-tutor-check --cpu 1 --cpu 2 --lifetime 600
```

状态目录必须尚不存在。启动器只监听随机回环端口，使用临时 API 密钥、两条 CPU 推理线程、单槽和 2,048 token 上下文；关闭 GPU offload、工具、Web UI 和提示缓存。本次 Web 检查请求最多生成 192 token，服务默认值也是 192；持有服务凭证的其他客户端可以在请求中覆盖该默认值。实际进程总量受 3 GiB 地址空间、300/310 秒累计 CPU 时间和最多 30 分钟生命周期约束。它不是常驻生产服务。

就绪后，目录中出现权限 0600 的 `runtime-connection.json`，包含供本地验证使用的连接参数。不要展示或提交此文件。只在显式本地开发中允许 `COACHING_ALLOW_LOOPBACK_HTTP=1`，并只允许数字回环地址；Docker 中的回环指向容器自身。生产容器使用前述 HTTPS 服务配置。

在该状态目录创建 `stop-runtime` 文件即可停止。正常停止会回收模型进程、删除连接文件和临时密钥，保留无密钥的运行身份与清理回执。硬杀父进程会终止子进程，但可能留下私密状态目录；维护者应在确认进程结束后清理该目录。

## 证据与质量边界

本地 Qwen 实际生成过错误的总体方差解释：它把题目约定的分母 `n` 错说成 `n−1`。加入明确的公开事实后，一次重放得到正确解释；真实浏览器中的严格正数累加提示也通过了人工检查。这些有限样例不能证明总体正确率，失败样例保留在验收记录中。模型输出始终是转义的建议文本，用户应对照公开契约与测试检查。

每日次数和全局并发是原子预约的资源政策，不是实测算力或费用。相同请求不会重复扣额，删除提示历史不会重置当天用量；正在执行时删除账号，返回结果不能重新创建正文或账号。个人计划、提示和响应纳入本人导出与删除，正式评测分数不受模型输出影响。
