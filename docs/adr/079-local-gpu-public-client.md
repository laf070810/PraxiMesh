# ADR-079：本地公开 GPU 命令的联合完成条件

日期：2026-11-02。状态：已采用，CLI 已连接，真实 GPU 验收待授权。对应 [RFC-024](../rfcs/024-local-cuda-practice.md)。

公开命令使用 `scripts/practice.py`，下载包先通过既有公开契约校验，不能选择镜像、socket、设备或状态。独立构建入口输出匹配当前源码和本机驱动的私有双镜像清单。首次物理准备复用 `judge/container_preparation.py`，不复制另一套 worker 初始化规则；本地练习继续拒绝使用 worker 或协调器状态。

`judge/container_practice_client.py` 组合原宿主状态、固定控制容器和有界管道。预期环境独立来自原配置与当前固定制品；原控制器另行观察设备和环境，二者一致才发送源码。每次公开运行调用完整启动检查；没有跳过、导入旧报告或上传本地成绩的接口。

收到完成帧及 EOF 后，仍须确认原附着进程和原容器成功退出、清理原控制器，再持原物理锁核对内部终态。公开运行必须产生新的同环境 completed 操作；恢复只允许原 job/reservation 从 pending 转为 recovered；维护不能改变其他操作。最后保存外层 completed 并重查取消，才能返回结果。

SIGINT/SIGTERM 只设置标志；停止和清理不依赖已取消的通信回调。SIGKILL 后沿原持久记录恢复，不重放源码。附着 Docker 客户端使用原私有 journal 下明确不存在的配置路径，拒绝任何意外文件；实际 Docker 验证该路径不会产生配置目录，因此崩溃不留下外部临时配置。

操作集中在[本地 GPU 指南](../deployment/local-gpu.md)。组件模型、实际 CPU Docker 管道和静态镜像构建分别记录在 [G69](../acceptance-g69.md)，均不代表当前 GPU 全路径通过，也不开放网站 GPU 提交。
