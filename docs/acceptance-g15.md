# G15：宿主 GPU worker 生命周期

2026-10-31。实现范围见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#常驻-worker-生命周期)与 [ADR-031](adr/031-gpu-worker-lifecycle.md)。操作统一在[GPU 部署指南](deployment/gpu.md#gpu-worker)。本轮遵循暂不占 GPU 的安排，只做控制流程、数据库和固定 CPU 验证。

## 已实现

新增 `gpu_worker` 宿主命令，显式指定完整设备 UUID、固定离线归档、worker 身份和私有持久目录。每次启动执行完整二十四阶段检查，之后在设备身份行锁下核对当前凭证、刷新健康并领取任务。执行、开始计费和结算前后也复核停止请求、凭证及数据库身份。CPU 健康记录不被 GPU worker 改写。

空闲时正常到期触发完整续检；文件缺失、时钟回拨或身份变化退出。取消、删除、租约丢失后，先沿原服务实例完成清理，再独立恢复；恢复前后均核对相同预约，恢复失败保留预约且不领取下一任务。中断执行保留已消耗额度，停止进程不自动把设备标为可用。

SIGINT/SIGTERM 处理器只赋值无锁标志，空闲最多每 100 ms 检查一次。常规回调执行清理；退出撤销当前所有者的凭证并停用设备，最后才由命令释放主机锁。`--once` 完整启动后最多领取一次，退出也撤销就绪。Django 的 `--skip-checks` 不能跳过 GPU 检查。日志只输出固定阶段、任务 ID 和错误类别。

## 检查与原始失败

- `test-results/g15-components-initial.log/xml`：既有定向回归 79 passed、4 个 PostgreSQL 专属场景 skipped；这些场景随后全部在实际 PostgreSQL 重放，未将跳过计为通过。
- `g15-worker-initial.log/xml`：24 passed、1 failed。所有者变化已经正确拒绝，但测试只预期沙箱异常，漏列已有的 `DeviceOwnershipError`；修正测试分类，运行时拒绝保持有效。
- `g15-signal-deadlock-initial.json`：固定自有子进程在持有 `threading.Event` 条件锁时接收 SIGTERM，实际复现处理器内 `Event.set()` 死锁；该子进程通过原 `Popen` 句柄终止并回收。修复使用无锁标志，信号入口拒绝带锁 Event。
- `g15-component-final.log/xml`：101 passed、无跳过。包含完整启动、旧预约、私有凭证、取消/删除、续检、身份变化、结算前凭证丢失、清理失败、停止与 CLI 配置。SIGINT/SIGTERM 使用实际自有 Python 子进程，核对退出和处理器恢复；CUDA 和服务执行方法明确使用测试替身。信号修正前 100 项通过记录保留于 `g15-component-before-signal.log/xml`。
- `g15-postgres-final.log/xml`：107 passed、无跳过。实际 PostgreSQL 验证领取及结算从身份检查到提交一直持有设备行锁，也重放发布/心跳竞争、旧预约、执行结算、控制与资源账目。临时数据库容器和卷已清理；首轮 102 项记录保留于 `g15-postgres-initial.log/xml`。
- 全仓 Ruff 和 Python 格式检查通过，命令 `--help` 与文档中的配置变量已核对。

以上无前缀文件均在忽略目录 `test-results/`。测试产生的文档与环境带有明确的合成身份，只存在于一次性测试目录，不是实际 GPU 运行凭证。

## 实际 CPU 控制链

`test-results/px-gpu-control-89b8833d82ec/result.json` 的四项检查通过：独立硬期限、主进程退出、控制器退出后的接管，以及拒绝终止身份不符的服务。前三项核对父组 2 核、2 GiB、零 swap 和 512 PID；四项清理均确认停止及 cgroup 为空。该脚本只启动平台固定的 CPU 控制进程，不执行学员代码、不访问 CUDA。

最终审查位于 `test-results/g15-final-audit.json`，核对 CPU 报告与当前源码摘要、四个自有服务均已卸载且无 cgroup、最终测试计数，以及变更文档的本地链接和锚点。

GPU 执行摘要为 `581b28f58d608260218ae64795843502879026c64e2d96a99c8af71832b53a4e`；CPU 策略仍为 `ae5cfea16ee6c6fa5cc76b5a8356fc3e30c27bfe8a86de27c8456ca55c2111c8`。

## 尚未验证的范围

没有运行完整 GPU worker 的 CUDA 路径，没有在用户数据库发布实际 GPU 凭证或开放网站提交。独占卡恢复后仍须重放完整启动、性能、设备恢复、总时限、真实队列与部署。GPU 容器化方案、题型/内容、页面、正式学习证据和可比榜单继续待完成；宿主命令不等于完整 Compose 支持。

本轮没有改变 schema、CPU 评测策略或界面，未重复 WASI、Compose、AI 与浏览器矩阵。没有修改共享 daemon、驱动或主机服务。完整 PRD 目标仍在实施。
