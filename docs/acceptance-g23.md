# G23 容器控制通信与 CPU 恢复

日期：2026-10-31。实现前基线为 `main` 的 `af0966b`。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)与 [ADR-039](adr/039-container-control-transport.md)，操作入口集中在 [GPU 指南](deployment/gpu.md#container-transport)。本批不访问 GPU、不调用 CUDA、不修改共享 daemon 或现有站点。

新通道通过受验证的完整容器 ID 启动固定桥接入口，复用既有消息协议。发送任务前核对原启动身份；容器内 socket 只存在于可信控制器的私有目录。消息不进入命令行、环境或 Docker 日志，任务容器不获得宿主挂载或 Docker socket。

## 已验证结果

真实通信重放记录为 `test-results/px-container-transport-e8537e37164f/result.json`，总 `passed`、`owned_image_tag_removed` 均为 `true`。九次调用全部通过，九个自有容器已停止并移除：

| 场景 | 实际观察 |
| --- | --- |
| 正常调用及最后的新调用 | 两次均完成 1 MiB 填充数据的双向传输；固定 CPU guest 返回随机挑战摘要，控制 socket 路径不可见；内部清理后才返回完成，再核对桥接器干净退出 |
| 错误启动身份 | 宿主拒绝不同的主 PID，未发送任务；容器端观察握手完成前断连，没有启动 guest |
| 超长帧 | 超过 4 MiB 的帧头被原消息协议拒绝，没有启动 guest |
| 运行中关闭连接 | guest 已进入固定忙等待；断连被控制器识别，实际 OCI 清理完成 |
| 客户端崩溃 | 只终止本次 Docker CLI；控制器实际收到 EOF，并完成 guest 清理，未把 CLI 退出直接当作清理证明 |
| 租约取消回调 | 等待消息期间回调中止连接，控制器观察断连并完成 guest 清理；这是实际进程通信与固定取消回调，不是 PostgreSQL 租约竞争验收 |
| 运行中额外输入 | `connected()` 拒绝额外消息，实际 guest 清理完成 |
| 独立硬期限 | 控制器故意不再检查连接；在显式停止前观察外层以 124 退出，记录的宿主进程身份全部消失，随后新调用成功 |

七次调用启动了真实 CPU guest，其中六次确认内部 OCI 状态及运行时进程清理，期限场景由外层独立终止。各次 guest 调用还记录宿主 PID/启动时间，在外层移除前核对消失。任务和随机探针数据不进入日志；结果仅保留固定身份与分类。

最终通信镜像为 `sha256:9c19e561ecb5943fe156df992241e7e28881aa067ab31b9dac1c44ec05274d15`，源码契约为 `6bc0b58f67a43ee9eca4e90ee60bfb2d1c1bb3c6503ff294041751ffb385e89a`。制品继续使用 G22 的固定 Python 镜像及完整 runsc 归档，报告记录逐文件源码摘要；构建和通信都使用空白 Docker 客户端配置及最小环境。

初轮组件检查 121 项通过；最终通信、运行时、监督器、受控命令、设备锁及发布检查共 175 项通过，无跳过。记录为 `test-results/g23-components-{initial,final}.log` 及 XML。Ruff 检查、533 个 Python 文件格式检查、迁移检查和既有 50 节点/26 题静态校验通过；静态内容检查未执行参考代码，见 `test-results/g23-{ruff,format,migrations,content}.log`。

同一镜像上的原 runsc 三组真实回归全部通过：隔离身份、输出/时间/临时存储及客户端退出后的四次新 guest 恢复，控制器退出和硬期限。九次内部 OCI 清理、三个外层容器和镜像标签移除均确认，见 `test-results/px-container-runtime-2a5e0e7d8240/result.json`。本次未修改普通监督器源码，因此没有重复 G21 的七类真实场景；其组件回归包含在最终 175 项中。

141 份 Markdown 的 777 个本地链接和改动文档的 35 个锚点通过，CLI 参数与集中操作入口一致。最终复核确认两份报告源码与当前文件匹配，12 个外层测试容器和两个镜像标签已不存在；原有 Web、worker、AI 三个服务仍健康。记录：`test-results/g23-document-links.json`、`test-results/g23-audit.json`。本轮两组真实矩阵首轮均通过，没有失败运行被计为成功。

## 范围

CPU 通道证据不能替代 GPU worker、设备身份映射、完整就绪、资源契约、CUDA 正确性/性能、容器化部署和升级验收；完整目标继续按[需求追踪](requirements.md)保留。现有 CPU/GPU 执行策略、正式准入、默认题库及数据库均未改动。本批没有重新执行 PostgreSQL、WASI 全目录、Compose 或浏览器矩阵，也没有重建用户现有站点。
