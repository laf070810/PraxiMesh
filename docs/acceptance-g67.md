# G67：本地固定入口、管道与当前镜像检查

日期：2026-11-02。基线 `b4027d2`，实现提交 `02bd734`。继续遵守“不占 GPU”要求，未查询 NVIDIA/NVML、初始化 CUDA、映射设备或运行 GPU 工作负载。

## 实现范围

固定控制入口 `scripts/container_practice_entry.py` 连接原私有状态的初始化、完整公开执行与显式恢复。模式、设备、随机挑战、期限、原状态和源码身份通过一次性握手绑定；任务镜像/驱动和完整环境确认后才发送源码。公开执行仍重跑二十四阶段启动，结果晚于原任务清理、终态保存和最后核对。恢复不构造执行适配器，不使用新任务镜像，也不签发就绪凭证。

有界非阻塞管道复用既有帧格式与字节预算；额外输入、断连、取消或期限到达终止正常执行。SIGINT/SIGTERM 只设置标志，客户端在发送源码前与 EOF 后再次检查取消。新入口已纳入 GPU/控制器/worker 源码契约，并补入应用 Dockerfile 的复制清单。设计见 [ADR-077](adr/077-local-gpu-controller-protocol.md)和 [RFC-024](rfcs/024-local-cuda-practice.md)。

这仍是可信控制器内部交付。宿主 CLI 对原控制容器的持久登记、成功退出核对、回收及真实 GPU 验收尚待完成，不能将完成帧或 EOF 单独作为可用的本地工具。现有 `compile` 只编译，`test` 仍只运行 WASI。

## 原始失败与组件证据

`g67-protocol-before.xml/.log` 中 12 项检查有 2 项失败：Python 相等比较错误接受布尔类型的阶段编号和环境设备索引。改为规范摘要比较结构身份后拒绝这两种混淆，完整检查通过。

源码清单核对另发现新入口不在应用镜像的 COPY 范围，记录为 `g67-app-sources-before.json`。这是静态打包缺口诊断，不是已运行的 Docker 构建失败；随后补齐复制，实际镜像结果另列。

| 范围 | 结果 | 原始记录，均在 `test-results/` |
| --- | --- | --- |
| 固定协议、原本地生命周期、控制器、启动、公开协议、编译与配置 | 267 passed | `g67-components-1.xml/.log` |
| 固定入口与两个真实子进程信号 | 8 passed | `g67-entry-1.xml/.log` |
| 发布包、冻结身份与单机初始化兼容 | 97 passed | `g67-packaging-1.xml/.log` |

最终三组共 372 项通过，无跳过。新协议使用真实双向管道连接完整生命周期、私有文件与锁，并验证帧截断、超限、背压期限、断连和额外输入；独立 Python 进程验证标准输入输出及无 Django/应用数据库依赖。GPU、daemon 与任务执行均为模型，不能据此声称公开 CUDA 样例通过。实际 SIGINT/SIGTERM 测试只针对本次创建的固定 CPU 子进程。

## 实际无设备控制镜像

按[控制端映射入口](deployment/gpu.md#container-controller)构建当前固定镜像，十项检查通过：原七项 UID/锁、挂载拒绝及三个方向的原任务恢复，加上初始化、公开执行和恢复入口对 v1 无设备状态的三项拒绝。新入口在创建任务/本地标记之前退出；这验证打包、入口和拒绝边界，不代表 GPU 正向运行。

记录为 `test-results/px-container-controller-f64c6dfcaf8c/result.json` 和 `g67-controller-1.log`。控制镜像 `sha256:fa6b6b00197ba2d075c6751fa45a41f8ea6d0492603512780dcade9aa4165840`，控制源码契约 `fac1df758db2be2b56d4919ae8f8efebd07985fca27640598e96dfd25e07452d`；CPU/runsc 任务镜像 `sha256:e0f78c6bc680a25b128e665091d4c2b5480c42351784e0f85b71a908491a453a`。18 个自有容器已清理，两个验收镜像标签与临时状态已移除；没有进行设备管理观察。

## worker、编译镜像与源码

当前 worker 镜像的十项真实无设备 Compose/PostgreSQL 预检通过，记录为 `test-results/px-container-worker-da8bca2888ff/result.json` 和 `g67-worker-1.log`。包括最小权限、配置保护、本地状态拒绝及重建；`gpu_queried`、`gpu_mapped`、`gpu_executed` 均为 false。管理制品只静态复制和核对，没有加载或执行；十一个自有容器及网络、卷、临时状态和验收标签已清理。

| 身份 | 值 |
| --- | --- |
| worker 镜像 | `sha256:45fc04e997df0d8df2d1b58a33060d00cabd57fce329aa5c3626f707e95c6198` |
| worker 源码契约 | `496d7126cbf140f12f6e3bdb56afc5b08a3c7b0e48d76085cabffb5f5b4b815a` |
| 无设备编译镜像 | `sha256:a376876edae098ab20397fc3d960e5c4caacdcaea166c6ccd71983f947ebcd6e` |
| 编译源码契约 | `42895b74117bf26f201835dd07550dde74e2023bd0931bb385cabf89f9f48464` |

新编译镜像中正确源码与语法错误两项真实 NVRTC 检查通过，记录为 `test-results/g67-compiler-build-1/build-result.json`，没有执行 PTX 或访问 GPU。编译镜像按构建入口约定保留，两个任务容器已清理。

`test-results/g67-identity-docs.json` 记录新增源码对各契约的影响，CPU/WASI 策略未变。只读核验原 `var/releases/2026-11-02-g59-cpu-2` 冻结 CPU 包通过，记录为 `g67-frozen-cpu.json`；未导入镜像、执行归档源码或改写制品。临时制作并核验 947 文件源码归档，新协议、管道、入口、策略、Dockerfile 和控制验收入口逐字节匹配，记录为 `g67-source-package.json`，临时目录已清理。

## 默认 Compose 与最终清理

按[默认 Compose 入口](deployment/compose.md)使用独立项目、私有 Docker 配置与临时端口完成完整回归，结果为 `test-results/px-compose-fce8c632ad9f/result.json`，启动日志为 `g67-compose-1.log`。镜像内 129 项真实 WASI 探针、七种题型的正确 Submit、错误答案及公开 Run 均通过；worker 离线后关闭提交，页面仍可使用；直接 IP/同源 POST 与跨源 CSRF 拒绝通过。

保留数据卷重建后，账号、草稿、会话、密钥、自有内容/撤回与九份评测记录均保留。首启与重建后的 Web/worker 都计算出当前 GPU 策略摘要 `c3dbf0578e987d57ed4d4bfcd94cdcd7303fc4e9d8ba6748fccda69a427f0561`，证明新增入口已实际随应用镜像交付。Web 未获得 Docker socket。未改界面或模型，本轮没有重跑可选 AI/浏览器矩阵。

最终逐个核对本次控制器、worker 及编译任务共 31 个容器均已移除，编译 journal 无活动记录；Compose 自有容器、网络和卷也已清理。编译镜像按构建约定保留。既有 Web、worker、tutor 的容器 ID、镜像及启动时间与 `g67-services-before.json` 一致，终态记录为 `test-results/g67-resources.json`。

完整本地 GPU 工具、当前 GPU 硬件/性能/故障恢复与部署、旧 F 应用整站升级及托管 CI 仍未完成。公开运营与许可证仍待维护者决定，完整 PRD 目标保持不变。
