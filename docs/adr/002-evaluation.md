# ADR-002：可信控制器与 gVisor 执行边界

状态：gVisor 策略保留，部署安全验证待完成；阶段 A 基础函数方案由 [ADR-004](004-wasi.md) 补充。日期：2026-10-22。依据：[RFC-001](../rfcs/001-phase-a.md)。

决定：独立 worker 通过数据库队列领取任务，只有固定 `python-function-v1` evaluator。每用例新建 gVisor 容器；受限的标准输入提供源代码和当前输入，预期答案保留在控制器。容器返回值不含可信评分；只有控制器比较结果能给 accepted。正式提交和公开运行区分记录。

默认禁用，镜像 digest、runsc、cgroups 等任何缺失都失败关闭。禁止普通容器回退、Web 进程 exec/eval、用户选择命令/镜像/挂载或将浏览器成绩作为证据。隐藏用例输出和堆栈不返回用户。

替代：microVM 运维更重，WASI Python 兼容性和构建待专项评估，普通 Docker/语言黑名单隔离不足。影响：每用例冷启动较慢；必须实测资源限制与清理；worker 是高权限可信组件，应运行在无业务凭证的专用执行节点并限制数据库权限。模拟测试只验证控制逻辑。

2026-10-24 兼容性修正：Docker Engine 的 CPU CFS 配额能力字段是 `CpuCfsQuota`，依据 [Engine API 版本记录](https://docs.docker.com/reference/api/engine/version-history/)及本机 Docker 29.9.2 的实际响应；原实现误写为 `CPUCfsQuota`。内存、交换区、PID 和 CPU 配额四项均须明确报告 JSON 布尔值 `true`；缺失、关闭或非布尔值继续拒绝，拒绝发生在创建容器之前。此修正不放宽资源约束，不改变显式选择后端的规则，也不构成真实 gVisor 或 GPU 验收。
