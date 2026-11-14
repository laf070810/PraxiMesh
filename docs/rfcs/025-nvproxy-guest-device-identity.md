# RFC-025：区分宿主与 nvproxy guest 的设备编号

日期：2026-11-15。状态：已实施并通过真实节点和两轮完整启动，见[验收记录](../acceptance-gpu0-startup.md)。背景见 [RFC-021](021-gpu-container-supervision.md)；决策见 [ADR-080](../adr/080-nvproxy-guest-device-identity.md)。

## 实际失败

获授权的 GPU 0 完整启动通过了初始 CUDA 正确性恢复，在资源身份检查失败。独立诊断显示宿主 UVM 为 `507:0`，guest 为 `254:0`；GPU UUID、型号、计算架构、物理卡 minor、只读根、能力、网络和环境字段均符合要求。现有验证错误地把宿主字符设备编号作为 guest 的预期值。

固定 `release-20261026.0` 的 [nvproxy 注册代码](https://github.com/google/gvisor/blob/release-20261026.0/pkg/sentry/devices/nvproxy/nvproxy.go)为 UVM 分配 guest 动态字符设备号；[VFS 分配器](https://github.com/google/gvisor/blob/release-20261026.0/pkg/sentry/vfs/device.go)从 254 向下选择。当前固定单卡运行配置下实际观测为 254；不能把此值外推到任意版本或其他设备配置。

## 实施边界

- 宿主设备选择、Docker 映射、OCI 输入、journal 和恢复继续保存实际 UVM 注册号。不得把宿主配置改为 254，或重新解释历史任务记录。
- 独立生成 guest 预期节点，固定当前已校验 runsc 二进制摘要及其 UVM 映射。未知二进制须拒绝该预期，重新审阅和实测后才支持。
- guest 仍必须恰好有原三项字符设备；卡和控制节点保持 195 major、原物理 minor 与 255。只修改 UVM 的 guest 预期值，不接受“宿主或 guest 任一值”，不采信 guest 自报的分配规则。
- 正式资源身份检查与固定设备节点探针共用这一投影；CPU 无设备路径保持原义。源码变化进入既有执行、任务和控制镜像摘要，旧镜像、旧报告不能充当新实现的通过证据。
- 独立诊断镜像和私有故障记录仅用于定位，不进入正式发布或替代完整启动。最终使用无诊断修改的源码重建匹配镜像并实测。

## 验证

先用实际观察的 254 与不同宿主注册号复现误拒绝；检查错误 guest major、卡 minor、节点集合与未知 runsc 均继续拒绝。随后运行相关组件、真实单卡节点传递/原任务恢复，以及完整启动和公开样例。资源阶段通过仍不代表正式队列、GPU 发布升级或公开内容发布已完成。
