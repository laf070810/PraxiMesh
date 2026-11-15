# ADR-094：首次 GPU 曝光确认丢失沿原实例续跑

日期：2026-11-15。状态：已采纳；真实 GPU 中断验收待完成。设计与复现见 [RFC-040](../rfcs/040-initial-gpu-exposure-acknowledgement.md)。

`GPURelease.recover()` 仅为尚无恢复尝试、内外阶段均为 exposing、当前库和曝光目标均为原 GPU 候选的操作，持久建立 expose_target 恢复意图。随后复用原实例检查和 GPUExposure 的完整来源、轮次、环境、准入与路由核对；不先撤回路由或停止活跃 worker。

原实例已退出则继续既有 maintenance 恢复，仍在原库完成清理、新预检、完整启动和新轮验证。身份核对失败保留现场，不以停止或重建代替核对。已完成发布的显式 recover、已捕获健康失败的 needs_recovery、曝光前失败与 CPU 目标行为保持原义。私有 schema 与已交付记录不变。

实现前的模型检查为三个活跃 worker 场景失败、三个已退出场景通过；修复后整站发布与曝光组件共 91 项通过。已有实际 CPU SIGKILL 场景增加了原 worker 启动记录不变的核对；Docker、数据库、设备和成绩仍为明确模型。原日志在 `test-results/apply-exposure-reply-loss-before-2026-11-15.log` 和 `apply-exposure-reply-loss-focused-2026-11-15-2.log`。首次组件运行因执行环境拒绝临时 Unix socket 而出现 49 项 setup error，授权环境下的重放完整通过，原失败不删除。

改动在独立工作区完成，不改变正在进行验收的 `6e40ba7` 冻结包。新制品与原自有站点上的实际 GPU 候选曝光中断仍须单独验证；本决定不开放公共 GPU 激活。
