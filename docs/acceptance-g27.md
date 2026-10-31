# G27 容器内 CUDA 编译与无设备验证

日期：2026-10-31。实现前基线为 `main` 的 `9c46271`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)和 [ADR-043](adr/043-device-free-container-compilation.md)，操作集中在 [GPU 指南](deployment/gpu.md#container-compiler)。本批没有使用 GPU、修改共享 daemon 或重建现有站点。

正式 CUDA runtime 与新增的无设备 guest 共用 `compile_ptx`，保留编译选项、PTX 上限和错误分类。固定编译镜像增加经过摘要校验的 NVRTC 库、原 LICENSE、只读 harness 及 guest 内的库搜索配置；基础 CPU 镜像继续独立构建。源码仅经有界通道送入 gVisor，可信外层只监督会话，不编译或执行学员输入。

## 实际编译结果

最终报告 `test-results/px-container-compiler-2d326571ddee/result.json` 的总结果与 18 个场景均为 `passed: true`，原始输出在 `test-results/g27-compiler-fourth.log`。共 18 个自有外层容器，其中两项非法请求在创建 guest 前拒绝；16 个 guest 均先报告 NVRTC 13.1、没有 NVIDIA 设备节点且 CUDA 驱动库不可加载。编译目标固定为 compute 8.6，不是从硬件测得的身份。

| 场景 | 实际结果 |
| --- | --- |
| 向量加法、矩阵乘法的两份参考实现 | 生成 PTX，返回有限大小与 SHA-256，未加载内核或执行 GPU |
| 六份语义错解 | 均生成 PTX；编译状态没有当作正确性或正式成绩 |
| 错误 C++ 语法、宿主 `main` 函数 | 由实际编译失败分为 `syntax_error`，清理 guest 后返回 |
| 不支持的目标架构 | 平台错误，未误记为学员语法错误或成功 |
| 额外 verdict 字段、布尔架构值 | `invalid_request`，未启动 guest |
| 编译后断连 | 原容器以固定失败码退出；显式 journal 恢复前原进程树已消失，随后新调用成功 |
| 编译后独立硬期限 | PID 1 以 `124` 退出，显式恢复前原进程树已消失；观察时间比握手中的原截止时间晚约 0.138 秒，随后新调用成功 |
| 错误后的新调用 | 使用新的容器和 nonce 重新完成编译与清理 |

完成与恢复均保留 `ready: false`，未生成 GPU receipt、写成绩、导入候选或连接现有数据库。成功轮的 18 个容器、临时状态目录和镜像标签全部清理。

| 制品与策略 | 固定身份 |
| --- | --- |
| 编译镜像 | `sha256:cc129660513fc4e0daffb98c92ce436889c8b073e472f1e923b357035c539c9f` |
| 编译源码契约 | `28ea0d9346ef7a8dc8f4d5b51b7707d54b10fef756178e7b28c5dd57204c3bd6` |
| NVRTC 归档 SHA-256 | `86077f16674ccea3e621ade9678887a37aca8f7500091aee4cabb1915e0c527b` |
| GPU 正式执行策略 | `9bbad302e604e38159ade1c774b7011cc9c13f533cb6605df41aacf85394597a` |
| CPU/WASI 策略 | `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，与 G26 相同 |

报告另记录两个编译库与 harness 的逐文件摘要。GPU 策略因共用编译实现变化而更新，不能沿用旧 GPU receipt；没有重新解释历史成绩。

## 原始失败与回归

首轮 `px-container-compiler-a79daa90cf5b` 在创建外层容器后被控制连接的明确类型检查拒绝，尚未运行 guest。已只把固定 `CompilerContainerService` 加入允许类型，并将其依赖加入可信控制镜像的源码范围；任意子类继续拒绝。边界测试先重现该失败，再通过 91 项连接/控制检查，见 `g27-connection-before.log`、`g27-connection-after.log`。

第二轮 `px-container-compiler-8925211ae4a9` 已完成真实 guest 的版本及无设备确认，但首次编译以平台错误退出。与原路径对照发现新 guest 没有配置 NVRTC 库搜索目录；补齐 guest 内固定 `ld.so` 配置及缓存后，真实参考解、错解和错误分类均通过。没有放宽 syscall、挂载、设备或资源限制；原报告未包含原始 NVRTC 诊断，不能从它推断具体 native 错误码。

第三轮 `px-container-compiler-ef15c1b212a3` 通过前 16 项，硬期限容器也以 `124` 退出，但测试错误地与总预约截止时间比较，遗漏监督器预留的 5 秒清理时间。改为比较原握手期限，并增加显式恢复前的进程树核对，第四轮完整通过。前三轮自有对象均已按原 journal 清理，原始失败报告和日志保留。

基础 runtime 的身份、输出上限、超时、临时存储及客户端退出场景，以及外层断连、硬期限回归通过，见 `test-results/px-container-runtime-cbcb7e4f3028/result.json`。使用重新构建的固定任务镜像 `sha256:b570c02c7be68077ebd5923fe3aeb5c8f0d47470a6755cf54edfec28e351f693`，三个外层容器的原进程树均已消失，测试镜像标签已移除。

首轮控制端回归 `px-container-controller-f3eb1309dcd7` 通过前六项，在最后的宿主到容器接管中误报完成文件缺失。保留的 `host-to-container/finished.json` 和 journal 显示新 guest 已完成并清理：观察器先查文件再查退出状态，可能漏过两次查询间发布的文件。确定性测试先重现该竞争，再改为先观察容器状态、后检查文件；缺少结果时仍拒绝成功。修复提交为 `ae0df44`，40 项控制端组件检查通过，见 `g27-observer-before.log` 和 `g27-observer-after.log`。

修复后的真实控制端回归 `px-container-controller-c467155b5c3b` 七项全部通过，原输出为 `test-results/g27-controller-second.log`。覆盖共用锁/UID、错误用户、替换锁目录、替换 socket，以及容器间、容器到宿主、宿主到容器的接管与新 guest 调用。控制镜像为 `sha256:8d229193d300862322f27cb8595e3ec69d4aac99882fa718c73a53315be42734`，源码契约为 `7d5078d6e1eabe26a2c096a41c7df1abbc87619a4c7107271e355c10f767795d`。15 个自有容器、两个标签和临时状态目录均已清理；本次修复只改变验收观察器，没有放宽正式运行时门槛。

编译、容器、正式控制器、历史成绩、凭证及发布兼容组件共 486 项检查通过、无跳过，见 `test-results/g27-components-final.log` 和 XML；后续观察器修复另按上述 40 项控制端组件及真实回归验证。最终 Ruff、561 个 Python 文件格式、无待生成迁移及既有 50 节点/26 题静态检查通过；内容检查没有执行参考程序。11 份变更文档的 331 个本地文件链接与新增部署锚点已核对。早期边界命令曾引用不存在的测试文件而未运行用例，原输出 `g27-boundaries.log` 保留，修正后的结果及最终回归分别记录。

独立只读审计 `test-results/g27-audit.json` 核对了四轮编译、基础 runtime 和两轮控制端的全部 70 个自有容器、9 个测试镜像标签均不存在，保留的原进程身份已退出。三份最终成功报告的源码摘要与当前文件一致，现有 Web、CPU worker 和 tutor 均运行且健康。日志与失败报告继续保留在忽略目录，没有删除其他对象或重建现有站点。

## 验证范围

这是实际隔离编译及生命周期验证，能够证明指定源码生成 PTX，不证明 CUDA 入口 ABI 可加载、内核运行正确、设备恢复或性能。外层总上限仍为 2 CPU、2 GiB、无 swap、512 PID，不能当作已交付的生产 guest 子配额。候选仍未发布，浏览器 CUDA 检查仍是前端结构提示，未改为服务器编译服务。

GPU 设备注入与占用监测、生产资源契约、容器后端的完整启动/领取/结算、真实 CUDA 正确性与性能、GPU Compose 和发布升级继续待完成。未重放完整 PostgreSQL、WASI、Compose、浏览器或 GPU 硬件验收；全目标继续保留在[需求追踪](requirements.md)。
