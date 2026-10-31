# G32：控制容器的 NVIDIA 管理查询

日期：2026-10-31。基线：`00666e5`，分支 `main`。设计见 [ADR-048](adr/048-container-nvml-observation.md)，操作见 [GPU 指南](deployment/gpu.md#container-device-observer)。本批只作维护者指定 GPU 0 的 NVML 元数据读取与固定 CPU guest 检查，不创建 CUDA 上下文、不运行内核。

## 交付范围

- 独立本地 NVML 快照、严格文件/版本清单及可选控制镜像构建；不加入 CUDA 计算库。
- 显式控制挂载契约 v3，绑定设备 UUID、minor、型号/能力/驱动、管理库和原宿主进程视图；原 v1/v2 继续可用，任务 journal 和资源契约未变。
- 固定有界查询子进程读取计算、图形、MPS 三类 PID；查询失败与不支持不回退为空闲。监测沿用原任务身份，清理后观察失效，不停止外部进程。
- 保留原控制端验收命令，新增可选管理参数；预计十七项实际场景，包括八项拒绝、正常管理/CPU 任务、v2 和七组原控制端检查。

## 已有结果与原始失败

相关容器组件、NVML 拒绝边界与旧发布包检查 **422 项通过，无跳过**，记录在 `test-results/g32-components-final.log/xml`。较早的 205 项旧组件、51 项新增观察检查及 148 项组合检查均通过，与最终集合重叠，不重复累计。

只读前提检查发现 `nvidia-smi --query-gpu` 不支持 `minor_number` 字段，改为从已核对身份的 XML 中读取；原始失败与宿主身份保存在 `g32-query-prerequisite.json`、`g32-host-observation-initial.json`。

首次验收调用误填了另一 UUID，发现后只取消本次尚在构建的自有 Docker 客户端；原进程已结束，未创建控制/任务容器或查询错误设备。记录为 `g32-initial-selection-cancel.json` 与 `px-container-controller-45d50ee57baa/result.json`，存储已清理；未生成的镜像标签删除返回 false，不能计作镜像曾成功构建。入口现会在构建前验证所选 UUID。

正确 UUID 的首轮实际镜像检查通过七项错误配置拒绝，但正常查询后因新增空 `nvidia-caps` 目录而失败：`px-container-controller-137b053f4b15/result.json`。自有容器、两个镜像标签和存储均已清理。使用固定源码与同一库快照的受限诊断确认 NVML 返回了正确 UUID、590.48.01 驱动、8.6 计算能力及三个空进程列表。修正后的相同诊断成功，见 `g32-diagnostic-fifth.log`、`g32-diagnostic-caps-fixed.log` 和逐次 JSON。

诊断前两次还分别暴露独立诊断脚本漏带日志压缩选项、通用基础镜像缺少私有 socket 父目录；只修正测试自有环境，没有改主机、正式控制镜像的既有权限或放宽未知设备。原失败日志和清理记录保留在忽略目录。

修复版本的完整镜像重放 **十七项全部通过**：`test-results/px-container-controller-76cb4bdfe1b6/result.json`、`g32-controller-observation-final.log`。八项错误配置在创建任务前拒绝；真实管理查询在私有 PID 控制容器中返回与宿主一致的身份及空列表，原 CPU 任务清理后监测对象失效；v2 和原七组 v1 场景全部通过。报告记录 27 个不同自有容器、两个镜像标签及临时存储清理，无清理错误。

| 制品/策略 | 身份 |
| --- | --- |
| 控制镜像契约 | `89237a16d7ef338faf855cbfacf903fd68da02af12ca7c3b261981a364a43732` |
| 实际管理控制镜像 | `sha256:84b6ccc38f8bde2b246d9950575177ee6cd880268a8539685212fceb28a260f8` |
| 实际 CPU 任务镜像 | `sha256:d3a70f8ab5852c9ad53501934428eab6f12d129974dd85070fc906ed9557883b` |
| NVML 590.48.01 制品清单 | `a622a9320c90a5e5c1ca73124eb8d6e93fcbd873ccb965674fa582f93e17aac0` |
| GPU 执行策略（未变） | `8f00cefb2df55242af185f186c4b653d6405ab4f05488d8e850a22e4cfe6b9af` |
| CPU/WASI 策略（未变） | `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192` |

最终报告的全部执行源码摘要与当前文件一致。原运行时、编译及驱动任务契约未变，完整身份记录为 `test-results/g32-policy-identities.json`。新管理接口仅进入独立控制镜像契约，尚未进入正式 GPU 适配器或凭证策略。

最终只读核对通过：`test-results/g32-audit.json`。包含失败和诊断记录在内的 40 个已记录自有容器均不存在，五个尝试使用的镜像标签均不存在，六组保存了出生时间的进程已消失；诊断标签下无残留容器，对应临时目录也已清理。现有 Web、worker 和 AI 三个服务均运行中且 healthy。该计数只包括保留了确切 ID 的对象，不将日志缺少 ID 的首次失败诊断纳入数量。

全仓 Ruff 检查与 595 个 Python 文件格式检查通过。新增文档后的 43 项发布/控制挂载复查通过，与上述 422 项有重叠；10 份相关文档的 395 个本地链接及唯一管理入口锚点有效。日志为 `g32-ruff-final.log`、`g32-format-final.log`、`g32-release-docs.log/xml`、`g32-document-links.json`；命令参数核对见 `g32-controller-help.txt`，均位于忽略目录。

## 验证边界

本次读取时 GPU 0 没有进程。空列表来自真实 NVML 查询，但不证明在私有 PID 容器中看到真实 CUDA 任务的宿主 PID，也不验证外部占用时的硬件场景。合成组件检查和 G31 的真实 CPU 归属分别证明其限定范围，不合并为 CUDA 证据。

此外，本批只覆盖三个明确的 NVML 上下文接口，尚未证明覆盖 `O` 类或无需上下文的 GPU 资源占用；正式准入前还需补齐来源及实测，不能把三个空列表当作穷尽的设备空闲证明。此限制与当前没有 GPU 进程的硬件验收缺口分别保留。

没有签发 GPU 凭证、发布 CUDA 题目、开放 GPU 提交或替换现有站点镜像。完整容器任务设备注入、CUDA 执行/故障恢复、正式环境、worker/Compose/发布协调器与本地 GPU 执行仍待完成；完整 PRD 保持未完成状态。
