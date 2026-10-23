# D3：Docker 资源能力预检修正

日期：2026-10-24。D3 在 [D 的功能与升级验收](acceptance-d.md)基础上修正 gVisor 的 Docker 能力识别，未新增题型、迁移或 GPU 执行入口。当前交付继续采用已实际验证的 WASI 后端。

## 修正与复现

原实现及原测试都将 CPU CFS 配额字段误写成 `CPUCfsQuota`。Docker Engine 使用 `CpuCfsQuota`，见 [Docker API 版本记录](https://docs.docker.com/reference/api/engine/version-history/)。修正测试输入后，旧实现出现 12 failed、9 passed；记录为 `test-results/d3-preflight-before.{log,xml}`。

现在使用正确字段，且内存、交换区、PID、CPU 配额四项都必须是 JSON 布尔值 `true`。缺失、关闭、字符串或数值不能代替布尔能力；资源不足时在检查镜像和创建容器之前拒绝。镜像摘要、runsc、有效容器限制与真实隔离探针仍是独立门槛。决定见 [ADR-002](adr/002-evaluation.md)。

相关预检、运行结果、策略、准入和筛选回归为 **114 passed、6 skipped**，跳过的六项均为 PostgreSQL 专属事务/锁测试；本轮未把跳过视为通过，也未重跑全部应用/PG/WASI 测试矩阵。修复前后回归仅验证控制逻辑，不表示已经实际运行 gVisor。成功回归记录为 `test-results/d3-preflight-regression.{log,xml}`。

## 新冻结制品与真实运行

- 发布目录：`var/releases/2026-10-24-d-candidate-3`。
- manifest SHA-256：`934d13c698047bc757b045b36656abbb69f037b814354b3142e63ce86f8e424e`。
- source ID：`a961662eaedb8d20ad8a7dcd46d449bc8e0a85f7048ac98c901b12e244ec301c`；448 个冻结文件。
- Web image：`sha256:5d1a14e4620b92c457c8b016cb98f2ab73b8396ecf45f5604d902b13c7ad6995`。
- worker image：`sha256:3a29e37e4aeda4d9359694a1975f33a489904794dc5bbd48150d40bfb5ef51ad`。
- 策略摘要：`ad2fb9940e6d99e09fbdd03d79588c6590b014c872cbc702c505ead7ea55616f`。
- WASI bundle 保持 `56d8e49398f508f793c71f5c350efbc27d27d219502aa8f9a7b8ca06b24c1c0b`。

源码冻结后逐文件核对，校验并导入真实镜像档案。共享策略指纹因这次修正改变，新镜像独立重跑 **123 个真实启动探针**。基础做题、中英、项目与本地工具浏览器，以及专业评测、博客、组织、AI 关闭状态和挑战五组扩展流程均通过；真实排空/恢复、错误制品拒绝、非 root/只读根/内部 worker 网络、账号删除和测试资源清理通过。证据归档于 `test-results/d3-release-final/`。

首次镜像尝试已通过全部启动探针，但宿主未设置已安装浏览器的缓存路径，Chromium 启动失败，尚未执行页面流程。失败日志与临时资源清理核对保留于 `test-results/d3-release-attempt-1/`。设置 `PLAYWRIGHT_BROWSERS_PATH` 指向已安装缓存并确认浏览器能启动后，使用相同不可变发布包重跑；没有改写应用、探针或镜像来消除该环境失败。

本轮固定镜像验收使用私有 Docker 网络和回环 HTTP。D 的源码 HTTPS、性能测量及 C4→D2 数据保留升级证据仍只属于原记录，不改写成 D3 重跑。D2→D3 的 26 个迁移文件和 8 个部署协调脚本逐文件相同；应用代码仅 `DockerSandbox.preflight` 的字段及布尔判定改变，WASI 执行器、题库、页面、队列和资源账本未改。差异与身份记录为 `test-results/d3-source-audit.json`。完成验收后只补写交付文档，不把后写文档计入冻结 source ID。

## 当前环境与尚未完成事项

本次受限命令环境不暴露 NVIDIA 设备；经只读宿主查询，实际存在四张 RTX 3090，驱动 590.48.01。安全 GPU 判题条件仍未满足：实际 Docker 29.9.2 未注册 runsc，且 `CpuCfsQuota=false`。本轮未修改共享 daemon、驱动或 GPU 配置，也未在 GPU 上执行提交代码。原始响应见 `test-results/d-external-capabilities.json` 和 `d3-docker-capability-contract.json`。

后续真实 gVisor/GPU 仍需要满足隔离与资源门槛的执行环境。gVisor 的 GPU 支持有特定驱动条件，并不增加独立硬件隔离，见[官方 GPU 说明](https://gvisor.dev/docs/user_guide/gpu/)。当前 Git 未配置远端，托管 CI、目标服务监管/容量、公有证书及续期尚未验收。维护者已明确先交付自部署版本，公开运营与许可证后续确定；全部 PRD 目标仍保留这些未完成项。
