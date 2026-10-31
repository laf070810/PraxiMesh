# G28 容器任务预算与内存超限恢复

日期：2026-10-31。实现前基线为 `main` 的 `6b87f9f`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)和 [ADR-044](adr/044-container-workload-budget.md)，操作集中在 [GPU 指南](deployment/gpu.md#container-workload)。本批不占 GPU，不修改共享 daemon、驱动或现有站点。

新增 `container-workload-v1`，整个任务容器共享 0.5 CPU、512 MiB 宿主内存、无 swap 和 256 个宿主任务上限，包含监督器、runsc 与 guest 的开销；guest `/tmp` 限制为 32 MiB。原 2 CPU/2 GiB/512 PID 配置冻结为 `container-supervision-v1`。新任务使用显式预算标识和 journal v2，原 v1 按原配置恢复，不能用新的上限重新解释旧记录。

## 实际资源与编译

`test-results/px-container-compiler-02d91cb34a68/result.json` 的总结果与 26 项场景均为 `passed: true`，原输出为 `test-results/g28-workload-fourth.log`。整个新任务预算内完成 G27 的 18 项编译/错误/故障场景，并增加四项资源检查及各自的新容器编译恢复。

| 真实检查 | 观察结果 |
| --- | --- |
| CPU | 3.001 秒墙钟时间消耗 1.542 秒 CPU，出现 30 个节流周期 |
| 宿主内存 | 内核记录一次 OOM kill，主动会话监测在确认原 guest 与进程树清理后返回 `memory_limit` |
| 临时存储 | guest `/tmp` 在写入 32 MiB 时返回空间不足 |
| 宿主 PID/线程 | 固定可信探针创建 253 个子进程后触顶，总任务数为 256；清理后剩余 3 个可信任务 |
| 故障后的新调用 | 四项资源检查后均以新容器重新完成真实 NVRTC 编译和清理 |
| 编译路径 | 两份参考解、六份语义错解、编译错误、非法请求、配置错误、编译后断连及独立期限均通过；语义错解可编译不代表答案正确 |

26 个自有外层容器全部清理，原进程树消失，临时状态目录与镜像标签均移除。完成与恢复始终 `ready: false`；没有签发 GPU receipt、发布候选题或写正式成绩。

| 身份 | 值 |
| --- | --- |
| 编译镜像 | `sha256:c6fdc0dcc1759788eb051a9fca730c45016b46822e4dde823885cb2f78df03a6` |
| 编译源码契约 | `8a6673a0f4db03726466f5f907eb71447300ec33c460860a36da167a1873ae0e` |
| GPU 正式执行策略 | `2060657ea4a66cbbda78a824b4819040edd1cd61e5e313db55d4922abfa0f103` |
| CPU/WASI 策略 | `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，与 G27 相同 |

正式 GPU 策略覆盖新的预算、内核计数和运行时清理实现，旧 GPU receipt 不可直接沿用。固定 runsc、NVRTC 及库的摘要继续由原构建入口验证，详见逐文件报告。

## 原始失败与修复

首轮 `px-container-compiler-4aa3933c9df9` 已通过参考解、CPU/存储/PID 及恢复，在内存探针中出现控制连接中断。只读查询原容器事件确认实际 OOM 和 `70` 退出码，记录在 `test-results/g28-memory-events-initial.json`；不能仅凭断连将其计为成功。

第二轮 `px-container-compiler-5e325e409938` 补齐固定探针诊断，确认内核已有 `max`/`oom`/`oom_kill` 事件且清理成功，但退出分类中的 runsc 状态查询失败。改为先核对原 guest 和运行时进程树清理，再产生内存 verdict；清理成功只通知一次。

第三轮 `px-container-compiler-1008c43f397f` 暴露另一种时序：沙箱已 OOM，但附着客户端未及时退出，原退出回调没有运行，最终误报 `time_limit`。修复为会话期间持续观察本 cgroup 的 `max`、`oom`、`oom_kill` 增量，并先完成原实例清理。仅全局 OOM kill、未知退出码或清理失败不能产生内存超限结果。第四轮完整通过。三轮失败的自有资源均已清理，原报告保留。

最初 216 项既有容器检查通过；新增预算和记录边界后 250 项通过。OOM 分类专项的测试替身曾遗漏位置参数，产生三项 `KeyError`；修正后 121 项通过，补上存活客户端与全局 OOM 边界后 124 项通过。失败和修正结果分别保留为 `g28-oom-classification.log`、`g28-oom-classification-fixed.log` 和 `g28-oom-monitor.log`。

最终容器、编译、正式 GPU 接口、历史成绩、凭证和发布兼容的 526 项定向组件检查通过、无跳过，见 `test-results/g28-components-final.log` 和 XML。Ruff、格式、无待生成迁移及原 50 节点/26 题静态检查通过；静态检查未执行参考程序。

## 兼容回归与剩余范围

本批原开发预算编译、持久 journal 升级、容器控制端和实际 PostgreSQL 预约恢复回归仍在进行，尚未将早期版本的结果当作本批通过。数据库验收入口已增加两种预算的 18 个真实场景，当前仅确认收集范围；未运行的场景不计为通过。

新预算是明确的容器总资源契约，不证明 GPU 显存、设备注入或 CUDA 运行正确性与性能。正式容器环境身份、启动/领取/结算、设备监测、GPU Compose 及发布升级仍待完成。未重放完整 WASI、Compose、浏览器或 GPU 硬件验收；完整产品目标继续保留在[需求追踪](requirements.md)。
