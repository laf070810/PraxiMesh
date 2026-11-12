# 私有构建目录的镜像源码权限

日期：2026-11-13。对应 PRD R-09/15；从 `95b24e4` 继续验证分数时钟修复后的匹配工具镜像，权限修复已提交并推送为 `fa58c7a`。只使用自有容器和固定公开源码，不查询、映射或执行 GPU，不导入旧 F 镜像。

## 已确认的问题与修复

原编译镜像的源码契约为 `0baaaf07…`，分数时钟修复后已变为 `53057eef…`，因此不能沿用旧镜像。以私有 `umask 077` 构建新编译镜像后，Docker 构建成功，第一次编译却在监督器启动阶段失败，结果没有标记 `checked`。原任务已经按原 journal 移除，没有活动记录。

检查一个从未启动的自有容器，确认 `/opt/praximesh/container_entry.py`、编译入口和基础模块均为 root 所有、权限 0600，固定运行用户 65532 无法读取。检查后删除该原容器。问题来自构建暂存沿用调用者 umask，既有只读根及非 root 配置无需改变。

基础 runtime、编译器、驱动和单卡任务的固定源码共用复制函数，目标文件明确设置 0644，内部源码目录设置 0755；固定驱动样例 JSON 同样可读。外层构建目录、Docker 客户端配置、锁和 journal 继续私有，原源码权限和调用者 umask 保持不变。没有递归放宽整个工作区，也没有修改运行身份、资源预算、seccomp 或设备准入。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md) 和 [ADR-043](adr/043-device-free-container-compilation.md)。

## 组件与实际构建

新增八项暂存检查覆盖四种镜像路径和 077/000 两种 umask，修复前全部失败；修复后连同编译、运行时、驱动、控制器及发布兼容检查共 **239 passed / 0 skipped**。这些检查验证真实文件字节和权限，归档提取及 Docker 使用明确替身。

在相同 `umask 077` 下重新构建，无设备编译器的正确源码和语法错误两项真实 NVRTC 自检通过。新镜像为 `sha256:b1cabef63b6ecf0485930e3ec6e07692841464191ca2427181c3ec37f090a2d9`，编译契约为 `89fef3ad23b99548a60aed436f34d82fbf83fe2bcd3b3baada8c6c353ab8f81a`。编译在 gVisor guest 中进行，只生成 PTX，不执行内核或产生正式成绩。

公开 CLI 的 **22 项全部通过**：两份参考实现、六份语义错解、语法/架构错误、镜像/并发/包拒绝、SIGINT/SIGKILL，以及原任务恢复和新编译；十四个原任务已移除。语义错解能编译不代表答案正确。

完整编译与预算检查的 **26 项全部通过**，覆盖原 0.5 CPU、512 MiB、零 swap、256 PID 总预算下的内存、CPU、存储和 PID 故障、逐次新编译恢复，以及断连和独立期限。二十六个原任务、临时镜像标签和私有临时存储均清理成功。

容器内可信控制端的 **14 项全部通过**，包括实际编译、源码发送前与编译后取消、控制器退出/强制终止、原任务接管及新编译恢复。二十六个控制/任务容器、两个临时镜像标签和私有临时存储完成清理。

匹配 worker 的 **10 项真实 Compose/PostgreSQL 预检全部通过**：最小授权、重建、显式/默认禁用、公开配置/本地状态/列权限越界/源码篡改拒绝、原源码恢复和旧控制配置拒绝。十一个自有容器、数据库卷和两个网络完成清理；没有查询设备或发布 GPU 就绪。操作统一见[本地编译指南](deployment/local-compiler.md)、[底层编译检查](deployment/gpu.md#container-compiler)和[容器 worker 指南](deployment/container-gpu-worker.md)。

## 匹配双镜像与最终清理

同一私有 umask 下，本地控制/任务双镜像静态构建及配置核验通过，生成的 `local-gpu.json` 权限为 0600：

| 用途 | 不可变镜像 ID |
| --- | --- |
| 本地控制镜像 | `sha256:1b9dea9ba317bf048f61e61e9177619c6f424cc543f84464dabe13b12bb0eff2` |
| 本地任务镜像 | `sha256:3d77b9954d3abcd8a6e7e8303d85ae6d615f4af5e6125a00691952b9468d7d30` |

任务镜像暂存的五十个 Python 文件均为 0644，内部源码目录可遍历，外层构建目录仍为 0700。构建只读取固定归档及受信系统库；没有初始化 CUDA、映射设备或运行 GPU 样例，结果保持 `ready: false`。

最终按原记录复核，**81 个自有容器均已消失**，三个编译工作区没有活动任务。临时镜像标签、worker 网络和卷均已清理；新编译器及本地双镜像按构建目的保留。原 Web、CPU worker 和 tutor 的容器 ID、镜像、启动时间、重启计数及健康状态与本轮开始完全一致。

## 源码身份与范围

CPU 评测策略与 GPU 执行源码未变；构建源码属于编译、控制器、任务和 worker 的镜像契约，因此这些镜像必须匹配新摘要。138 个相关源码文件已与 `fa58c7a` 的原始 Git 对象逐字节比较一致。已有冻结发布包保留原内容与验收范围，没有原地更新。

GPU 完整启动、公开样例执行、性能、队列及整站部署仍待授权后的硬件验收；旧 F 应用升级、专用部署节点和维护者外部事项继续按[需求追踪](requirements.md)保留。当前不能将完整 PRD 标记完成。

## 原始证据

私有记录集中于忽略目录 `test-results/current-tool-images-2026-11-13/`：

- `preflight.json`、`compiler-build-process.json`、`compiler-failure-state.json`、`compiler-failure-cleanup.json` 和 `compiler-image-permissions-before.json` 保留原构建失败、文件权限及清理身份。
- `staging-before.xml`、`staging-components.xml` 和对应日志分别记录修复前八项失败与修复后组件结果。
- `compiler-fixed/build-result.json`、`compiler-fixed-process.json` 和 `fixed-contracts.json` 保存新镜像、真实自检及源码契约。
- `compiler-cli/result.json` 与 `compiler-budget-process.json` 记录公开 CLI 和完整资源检查；预算检查的原报告为 `test-results/px-container-compiler-1cc85b59748a/result.json`。
- `controller-compiler-process.json`、`worker-process.json`、`local-images-process.json` 和 `final-resource-audit.json` 保存后续检查与最终资源审计；原控制器和 worker 报告分别为 `test-results/px-controller-compiler-8aeb487b4e1e/result.json`、`test-results/px-container-worker-8ae56baea409/result.json`。
- `committed-source-match.json` 记录相关源码与已推送提交的逐字节比较。

修复前后使用不同输出目录，保留原失败镜像和证据；没有通过改写旧构建结果或重用旧凭证取得成功。
