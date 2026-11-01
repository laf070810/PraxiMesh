# G45：GPU worker 的发布代配置身份

日期：2026-11-01。范围：原节点私有文件、配置读取、启动边界与冻结制品兼容；未查询设备、运行 GPU、加载镜像或修改现有服务。对应 PRD R-09、R-16；[设计](rfcs/022-gpu-release-coordination.md)、[决策](adr/061-generation-bound-worker-config.md)、[集中说明](deployment/container-gpu-worker.md#generation-configuration)。

## 变化

- `judge/container_deployment_config.py` 核对原 owner、完整配置摘要、发布代/数据库/worker 角色、worker 镜像契约，以及原 mounts、目录、安装和锁 inode。记录必须是有界、无链接、本人所有的 0600 文件；未绑定的独立安装保留原配置行为。
- `container_gpu_worker` 比较 Django 初始化时的完整配置与代际快照，并在取得原设备锁后重读、再比较。配置或仅代际记录变化都在启动探针、领取任务和生成凭证之前拒绝。
- `GPUDeploymentNode.generation_configuration()` 在原锁及所有权核对下，从支持本协议的冻结发布代生成私有配置和记录。驱动/管理清单须匹配原节点，数据库登录限定为同实例 worker 角色；生成不写入、不切换数据库、不启动 worker。
- 新模块已纳入 GPU execution/controller/worker 源码范围和 worker 构建复制路径；CPU 策略源码与父提交 `499f18d` 相同。后续 GPU 镜像与凭证必须按新身份重建、重新验证，不能使用旧就绪记录。

## 复现与验证

`test-results/g45-before.xml` 在修复前记录两项失败：已绑定目录缺少代际记录仍被读取，配置在取锁前变化仍继续启动。这些用例使用合成设备和显式运行时替身，不是硬件执行。

最终十个相关模块 **371 passed、0 skipped**，见 `test-results/g45-components.xml` 和 `.log`。包括真实临时文件/原锁、绑定与数据库角色错配、配置或元数据改动、链接/权限/大小/重复字段拒绝、安装/目录/锁更换、镜像契约不匹配、仅生成不发布、旧 worker 配置、容器与宿主 worker 生命周期、凭证及发布读取兼容。早期 97 项与 144 项结果保留，不与最终数字相加。

当前代码重放原节点绑定的九项真实文件锁/子进程检查全部通过，见 `test-results/g45-attachment-current/result.json`：包含四处 SIGKILL 后按原意图继续、跨进程锁、复制协调器及旧记录拒绝。全部 69 次 Docker 调用经受限包装器确认只有 `info`；自有私有状态已清理，没有启动 worker、数据库或容器。设备元数据与初始化站点为合成夹具，不是代际切换或 GPU 验收。

只读核验两份未改写的旧归档，见 `test-results/g45-old-archives.json`：

| 制品 | 可信清单 SHA-256 | 结果 |
| --- | --- | --- |
| `2026-10-24-f-candidate-1` | `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` | 464 个源码文件、两个镜像及完整层校验通过 |
| `2026-11-01-g42-candidate-3` | `44b6ca03b1e22d92382e5a04b578dee64813f57eceb1b3c3485117b0fdfa2142` | 839 个源码文件、四个镜像及原 GPU 契约校验通过 |

这证明旧格式读取兼容，不是重新导入或执行。两份旧包均不含新的代际协议，不能把它们当作新协调器 worker 配置的执行证明。当前源码与策略范围记录在 `test-results/g45-policy-identity.json`。

## 仍未完成

本轮没有发布双文件切换命令；原 worker 实际停止、原数据库预约清理、原 journal 恢复、配置切换的持久事务、GPU 代际生命周期及真实队列/硬件验收仍待完成。`apply` 继续拒绝 GPU 四镜像激活，正式 GPU 内容和入口保持原门槛。

[G44](acceptance-g44.md)的 Web 端口恢复真实复验仍等待测试镜像缓存恢复许可；本轮没有再次尝试被审批拒绝的镜像加载。完整 PRD、托管 CI/runner、长期部署目标与授权政策仍按[需求追踪](requirements.md)保留未完成项。
