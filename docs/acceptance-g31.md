# G31：可信控制容器的宿主进程观察

日期：2026-10-31。基线：`84f5658`，分支 `main`。设计见 [ADR-047](adr/047-container-host-process-observation.md)，操作见 [GPU 指南](deployment/gpu.md#container-process-observer)。本批只运行固定 CPU 任务和 gVisor/NVRTC 编译，不查询 NVIDIA 进程、不注入 GPU 设备、不初始化 CUDA。

## 交付与身份

新增显式的控制端挂载契约 v2 和 `HostProcessView` / `OwnedProcesses`。核对宿主 procfs inode/dev、boot ID、所有子挂载只读与私有传播；只读取有界 stat/status/cgroup。任务归属绑定完整核对后的 PID 1、出生时间、原 journal、容器及启动实例。原 v1 控制端默认不启用，任务 journal v1/v2 和预算定义不变。

| 制品/策略 | 当前身份 |
| --- | --- |
| CPU/WASI 策略（未变） | `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192` |
| GPU 执行摘要 | `8f00cefb2df55242af185f186c4b653d6405ab4f05488d8e850a22e4cfe6b9af` |
| 编译契约 | `e7b16b429facf2a14fb3811c9b8e8446ed9ec77d414e5ec46a5d7e187ba70983` |
| 控制镜像契约 | `6653de7ef8dd28d7557f69415fbfc921efa0035354a774f657a80aafe40f8887` |
| 实际编译镜像 | `sha256:f2fb0f0396572220e1cd1ae12fb1a0de4ffd3697821190a59da40bcbdfaafed0` |
| 实际控制镜像 | `sha256:7f08a057729231430523a831dd6aa3359e040f9f756c43c6cc00a261e8a09f3a` |

完整身份在 `test-results/g31-policy-identities.json` 和实际报告。GPU 摘要随共享监督代码中新增的已核对 PID 字段更新；没有签发 receipt 或修改 GPU 提交准入。

## 前提失败与修正

- 非递归 proc bind 被当前 rootless 环境拒绝为 EINVAL：`g31-proc-prerequisite.json`。
- 递归只读选项未同时指定 rprivate 时，Docker CLI 在创建前拒绝；初次及带诊断重放分别保留为 `g31-proc-recursive-prerequisite.json`、`g31-proc-recursive-diagnostic.json`。
- 显式 `bind-recursive=readonly,bind-propagation=rprivate` 后真实固定 CPU 检查通过：`g31-proc-rprivate-prerequisite.json`。控制器私有 PID 为 1，宿主 PID 不同；任务、控制器、外部固定进程的 cgroup 分别可辨，实际路径包含 `..`，因此代码不将它用作文件路径。

以上文件位于 `test-results/`，对应日志和固定探针源码一并保留。四次前提检查的六个自有容器均执行了清理；没有通过增加能力、host PID 或修改 daemon 来解决挂载限制。

## 实际验证

`test-results/px-controller-compiler-6ca16e8bcaa4/result.json` 与 `g31-observer-acceptance-initial.log`：十八项全部通过。四项错误挂载/身份检查在任务创建前拒绝；十四项原编译/取消/退出/终止/恢复场景在 v2 控制端上重放。七次存活编译任务实际观察 PID 1 与其成员，拒绝宿主 PID 1、控制端自身，并在正常清理后拒绝旧观察对象。30 个自有容器、两个镜像标签和临时目录已清理。

组件与发布兼容检查：`g31-components-final.log/xml`，356 项通过，无跳过。GPU 原凭证、准入、历史证据和恢复边界：`g31-gpu-boundaries.log/xml`，190 项通过，1 项实际 PostgreSQL 锁竞争检查因本组使用 SQLite 而跳过；该项未计作通过。本批没有数据库结构或事务改动。

原 v1 控制端的七组共用锁、UID/挂载拒绝、跨容器/宿主接管全部通过：`test-results/px-container-controller-399553819cb1/result.json` 与 `g31-controller-compatibility.log`。15 个控制/任务容器清理完成；仍使用原无宿主 procfs 的 v1 挂载清单，任务 journal 与 v2 总预算接管正常。

只读最终核对在 `test-results/g31-audit.json`：两份最终报告中的全部源码摘要与实现提交 `3ac643a` 匹配，合计包括前提检查在内的 51 个自有容器、4 个镜像标签均不存在，16 组记录进程已消失。报告未保留未清理目录；原 Web、worker、AI 三个服务均运行中且 healthy。

全仓 Ruff 检查与 586 个 Python 文件格式检查通过；新增验收文档后另有 36 项发布兼容检查通过，9 份相关文档的 344 个本地文件链接及进程观察锚点有效。结果为 `g31-ruff.log`、`g31-format.log`、`g31-release-docs.log/xml`、`g31-document-links.json` 和 `g31-observer-help.txt`，均位于 `test-results/`。

本批不证明 NVIDIA 返回的 PID 来源、GPU 空闲、CUDA 故障恢复或性能正确。单卡设备接入、完整 GPU 启动/worker、正式环境和 Compose/发布协调继续待完成。
