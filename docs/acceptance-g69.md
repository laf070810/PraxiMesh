# G69：本地 GPU 公开命令与实际 CPU 管道验证

日期：2026-11-02。对应 PRD R-09/15/16、§8.5；起始提交 `c750b62`。继续维护者“不占 GPU 的开发”要求，不执行 NVIDIA/NVML 查询、CUDA 初始化、设备映射或 GPU 工作负载。操作集中在[本地 GPU 指南](deployment/local-gpu.md)，设计见 [ADR-079](adr/079-local-gpu-public-client.md)。

## 本次连接

公开 CLI 接入严格下载包校验、原客户端/物理状态、固定控制器和有界管道。构建入口生成当前源码对应的控制/任务双镜像清单；首次物理准备与 worker 共用原函数，既有准备规则保持原义。新 privileged 模块纳入 GPU 有序执行摘要和控制/worker 源码；CPU 执行策略单独核对。

宿主独立计算预期环境，与原控制器观察结果一致后才发送源码。每次公开运行执行完整 24 阶段启动检查。收到完成帧及 EOF 后，还须确认原附着进程和容器成功退出、原容器删除及新内部操作 completed，再持久保存外层完成并检查取消。恢复继续原操作身份，不重放学员源码，不生成正式成绩或就绪凭证。

## 组件和真实 CPU 容器

`test-results/g69-components-final.xml` / `.log`：**505 通过，1 跳过，0 失败**。覆盖私有清单、公开 CLI、实际子进程管道、原状态/控制器、完整本地流程、适配器、worker 配置及发布兼容。唯一跳过为 `test_real_local_public_tests_are_private_and_not_formal`，需要显式验证的 WASI bundle；本轮不把它算作通过。

原始失败保留在 `g69-client-before.xml`、`g69-cli-before.xml` 和 `g69-cli-second.xml`：恢复替身错误地把整个返回字典作为 status；新增子进程替身导入了错误常量并间接依赖未配置的 Django；脚本测试断言了错误的诊断前缀。分别修正替身与断言后重放通过。实现复查同时修正公开 CLI 读取不存在的 `exercise_type` 字段，统一使用包的 `evaluator`；CLI 包装保留独立脚本调用与安全错误分类。

`test-results/g69-client-docker-1/result.json`：**五组真实 CPU Docker 场景通过**，分别为正常完成、成功帧后的非零退出、额外输出、取消和宿主客户端 SIGKILL。每组随后沿原状态显式恢复；总计 10 个控制容器。使用固定 CPU 测试入口和合成设备声明，没有映射或查询 GPU，也不执行学员源码。

实际检查确认 Docker 附着客户端不会创建显式指定的私有配置路径；被 SIGKILL 后没有遗留外部配置目录。取消和非零退出扣留成功结果，恢复先停止原控制器，再处理原内部状态。全部测试容器、私有测试状态和测试镜像标签已清理；原 Web/worker/tutor 的 ID、镜像和启动时间保持一致。

该轮 CPU 测试镜像为 `sha256:410a141c01f01d2e1560546ec31d3c6dce8bac5dc0d68869be4b6620d9c638d7`。控制契约为 `42a93c51357fa7884be51d21eefa09ea11e0fb6b20617c186f82c5c36b84ed5e`，GPU 执行摘要为 `3da09d15b8812aaeb5ff5ed1c5d13d6c6e0ac20dd1ac478fe716ddff81be0048`。测试镜像不可用作真实 GPU 控制镜像。

## 公开构建入口与源码交付

实际执行 `scripts/build_local_gpu.py`，仅静态复制受信系统驱动库并使用固定 Docker CLI、runsc 和 NVRTC 归档。`test-results/g69-local-images.log` 及 `g69-local-images/` 保留完整构建日志和私有清单；构建后再次通过当前源码与镜像配置核验：

| 制品 | 不可变镜像 ID |
| --- | --- |
| 控制镜像 | `sha256:3508016d11e052d207679989b198bd28ed576f9cf873dba940b724e7c52ce477` |
| 单卡任务镜像 | `sha256:098847cfd8e4e8a07d93f0aeab49abb7e1634ee80930036cadebfda0bfa0571f` |

两镜像刻意保留在本机，供后续获得授权后验收；没有启动这两个镜像中的设备入口。任务源码契约为 `23d04b7743abf7c920bfd105548582c380b33079cdfdfbd6bd67f04c3f8c7b70`，控制与 GPU 执行身份与上方 CPU 管道检查一致。`g69-resources.json` 另行确认 10 个测试容器及 CPU 夹具镜像已删除，原三个站点服务未变。

`g69-identity-docs.json` 确认 CPU 策略字节与起始提交一致，GPU 新模块按有序列表进入契约；compiler/runtime/task 因 GPU 策略文件变化需要匹配的新镜像，不能复用旧身份。`g69-source-package.json` 记录 968 个文件的实际源码快照、归档和逐字节读取校验，新模块、公开构建/调用入口、CPU 验收工具和集中操作文档均包含在内，临时目录已清理。全仓 Ruff 检查和 798 个文件的格式检查通过。

## 页面与剩余范围

只读比较 `9da2bdd` 至当前的 `templates/`、`static/`、`frontend/`、`locale/` 和编辑器浏览器检查，源码相同。[G61](acceptance-g61.md) 的中英 25 页 × 320/390/768/1440 四种宽度，共 200 组布局证据仍对应这些界面源码，覆盖标题边界、筛选控件和复选框对齐及横向溢出。本轮核对原报告，不将其写成一次新浏览器实跑。

完整本地命令已连接；真实 GPU 启动、公开样例正确性、性能与故障恢复尚未执行，正式 worker/队列/部署升级亦保持待验收。CPU 管道替身、静态镜像构建和组件模型均不能替代这些要求。网站 GPU 准入没有开放，完整 PRD 目标继续保留。
