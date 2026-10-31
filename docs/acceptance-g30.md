# G30：本地驱动制品与无设备容器验证

日期：2026-10-31。基线：`640285b`，分支 `main`。设计见 [ADR-046](adr/046-local-cuda-driver-artifacts.md)，操作入口见 [GPU 指南](deployment/gpu.md#container-driver)。本批遵循“不占 GPU”要求，没有调用 CUDA 初始化、分配上下文或执行 PTX。

## 交付范围

新增私有驱动快照和独立运行镜像：从已安装的系统驱动静态复制四个 CUDA/PTX 库，核对 root 所有权、写权限、版本、ELF/SONAME、大小及 SHA-256。构建前后重新检查安装文件；guest 加载前验证完整清单及别名。源码摘要与驱动清单共同绑定镜像，原 journal v1/v2、无驱动编译接口及总预算不变。

本机 NVIDIA open 驱动为 `590.48.01`；固定 `release-20261026.0` runsc 的实际支持列表包含此版本。镜像中的 guest 仅动态加载四库、解析固定 CUDA API 符号，再使用原 NVRTC 13.1.115 编译路径。没有将库加载作为 CUDA 正确性或性能证据。

| 制品 | 身份 |
| --- | --- |
| 驱动源码契约 | `a808b3cc89ceb99adbec3d9e1504c59644c7c5817f33149f531ec62340b68447` |
| 驱动清单 | `4ae10781d267e67b90478d5754b2fde98b80dbf037efe54cd15395ee73e1586e` |
| 合并镜像契约 | `4eec40695257ad052c614896ef93fd388a98bc6c6af97549fc5921d3c14671de` |
| 实际镜像 | `sha256:09fd5644b7d25f58542d2e27c69fa546e2d95008947472fcd69263f2a05ed2e7` |

各库摘要、SONAME、字节数与完整源码身份保存在实际报告；快照位于忽略目录，未提交驱动二进制。原 CPU、GPU、基础运行、编译及控制镜像契约均与 G29 相同，记录在 `test-results/g30-policy-compatibility.json`。

## 原始失败与修复

首次报告：`test-results/px-container-driver-c93cf169f794/result.json`，日志：`test-results/g30-driver-acceptance-initial.log`。runsc 的精确版本检查通过；guest 在加载库之前退出。原因是准备消息漏带共用解析器要求的 `protocol: 1`。修复为共用准备消息构造器，并用实际 guest 解析函数验证序列化后的请求；同时给固定探针记录失败阶段。没有放宽消息协议、版本检查或库校验。

失败任务的 guest 与外层容器已沿原持久记录清理，镜像标签及临时运行目录已删除；原始报告与构建上下文保留。

## 验证边界

组件报告为 `test-results/g30-components-final.log` 和 `.xml`：250 项通过，无跳过，涵盖驱动来源/篡改/协议、原编译接口、监督、持久恢复及发布包。

真实无设备重放报告为 `test-results/px-container-driver-9a271c38cdd4/result.json`，日志为 `test-results/g30-driver-acceptance-final.log`：五项场景全部通过。错误驱动清单、无驱动编译接口使用新镜像均在创建容器前拒绝；完整检查、整任务 SIGKILL、清理后新调用三项沿同一安装身份及 v2 记录完成。两个完整调用分别通过库加载、向量加法编译、矩阵乘法编译、语法错误和恢复编译；共十一轮 guest 加载，不初始化 CUDA。三个任务容器、镜像标签和临时运行目录已清理。

审阅后补齐验收工具对未完成或不可读 journal 的保留分支，三个对应组件检查通过。当前入口的最终重放为 `test-results/px-container-driver-615860808bfa/result.json`，日志为 `test-results/g30-driver-acceptance-cleanup.log`：五项再次通过，所有记录的源码摘要与提交 `8c93b76` 匹配；驱动镜像 ID 和契约未变。

只读最终核对见 `test-results/g30-audit.json`：三份报告共七个测试容器、三个镜像标签均不存在，六组记录的原进程已消失，三个临时运行目录已清理。两个通过报告中，恢复前后的三份编译元数据逐项一致；这只比较 PTX 编译结果。现有 `praximesh-web-1`、`praximesh-worker-1`、`praximesh-tutor-1` 均为运行中且 healthy，未重建或停止现有站点。

Ruff 检查和 582 个 Python 文件格式检查通过；十份相关文档的 346 个本地文件链接及新增检查入口锚点通过，CLI 参数与指南一致。结果在 `test-results/g30-ruff.log`、`g30-format.log`、`g30-document-links.json` 和 `g30-driver-help.txt`。

本批不改变数据库、正式 GPU 执行策略或提交准入，不重新验证已有页面、CPU/WASI、模型或公开部署。四库配置是否覆盖实际 PTX JIT，仍需硬件重放；设备注入、持续占用与 PID 对应、完整容器 worker、Compose/发布协调及候选题型发布继续待完成。
