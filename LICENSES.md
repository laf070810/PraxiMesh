# 发布授权状态

PRD §13 将代码许可证、题目/教程文字、社区投稿和外部测试/数据授权留给维护者最终确认。当前未替维护者授予许可证。

- 平台代码：拟议选项 Apache-2.0 或 MIT，待维护者确认。
- 本仓库原创教程/题意：可考虑 CC BY 4.0；参考实现和测试代码需单独约定代码许可。
- 社区投稿：贡献者必须有相应权利，并接受已确定的投稿条款；目前条款尚未生效。
- 第三方依赖：按各自许可证使用，镜像发布需保留相应声明，不能以平台许可证覆盖。

浏览器编辑器依赖 CodeMirror、Lezer、Ruff WASM、Prettier、clang-format WASM 及其运行依赖。完整依赖声明随构建保存在 [`static/editor/THIRD_PARTY_LICENSES.txt`](static/editor/THIRD_PARTY_LICENSES.txt)，包括上游附带的第三方声明，以及 LLVM、Emscripten 和 musl 的[补充许可文本](frontend/clang-format-NOTICES.txt)。CUDA 语法由锁定的 Lezer C++ 语法扩展生成，保留其 MIT 声明。npm 锁文件固定来源与完整性，构建入口见 [编辑器指南](docs/editor.md)。

当前 WASI bundle 保留 CPython LICENSE、QuickJS 的 MIT LICENSE，以及固定 WASI SDK 24 对应的 wasi-libc、musl、cloudlibc、LLVM compiler-rt 声明。原文和不可变来源/摘要见 `vendor/wasi/`、`vendor/quickjs/`。安装到镜像的 Python 包保留各自 distribution 许可元数据。这些记录不替代维护者对最终公开制品授权和再分发义务的复核。

容器内 runsc 的开发验收镜像保留固定 `release-20261026.0` 的 [gVisor LICENSE](vendor/gvisor/LICENSE)（含所附第三方声明），镜像位置为 `/opt/licenses/gvisor-LICENSE`。可信外层的 syscall 配置基于固定 [Moby 配置与 Apache-2.0 声明](vendor/moby/README.md)，本项目的附加调用单独记录，不改写上游文件。这些许可不改变平台代码和内容的待确认授权状态。

可信容器控制端固定 Docker CLI 29.9.2，仅提取官方归档中的客户端；其 [LICENSE、NOTICE 与来源说明](vendor/docker-cli/README.md)保留在镜像的 `/opt/praximesh/vendor/docker-cli/`。归档及二进制摘要在构建和启动时核对，不安装或启动新的 Docker daemon。

无设备 CUDA 编译镜像使用固定 NVRTC 13.1.115 归档中的两个运行库及别名，并保留归档原有 `LICENSE`：外层镜像路径为 `/opt/guest-root/opt/nvrtc/LICENSE`，guest 中为 `/opt/nvrtc/LICENSE`。此工具不加入 CUDA 驱动；基础 CPU 镜像不携带 NVRTC。平台的待确认授权不覆盖 NVIDIA 的许可条款，公开制品仍需维护者复核。

可选 Compose 模型镜像保留固定 llama.cpp 来源的 MIT 许可 `/opt/LICENSE.llama.cpp` 与固定 Qwen3-0.6B-GGUF 版本的 Apache-2.0 许可 `/opt/LICENSE.qwen`。源码、权重及许可下载均校验固定摘要，见 `scripts/build_compose_tutor.py`；不改变平台本身的待确认授权状态。

`LicenseRef-Pending-Maintainer-Approval` 是开发状态标记，不是开放许可证。此项不阻止本地研发，但阻止宣称已经完成开源公开发布。
