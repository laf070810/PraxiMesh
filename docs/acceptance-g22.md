# G22 容器内 runsc 的 CPU 验证

日期：2026-10-31。实现前基线为 `main` 的 `1d4907f`。本批只使用固定 CPU 探针，不读显卡、不执行 CUDA、不生成 GPU 凭证。设计见 [RFC-021](rfcs/021-gpu-container-supervision.md)及 [ADR-038](adr/038-nested-runsc-container.md)，操作统一见 [GPU 指南](deployment/gpu.md#container-runtime)。

## 实测与实施依据

默认非 root、无能力的只读监督容器中，`unshare(CLONE_NEWUSER)` 返回 EPERM，cgroup 子树不可写；自有容器已移除，见 `test-results/g22-namespace-prerequisite.json`。单独提供经审阅的 syscall 配置后，同一固定程序成功创建用户命名空间，见 `test-results/g22-userns-profile.json`。两项检查都没有修改共享 daemon 或主机服务。

完整固定 runsc 归档、固定 Python guest 根目录及外层监督器的原型镜像为 `sha256:f758a35c76c78a188f6050f4079eb04bf521edc5eae8c2739d12aeb2298da876`。首次 runsc 启动失败；完整诊断定位为 gofer 的 `chroot` 被外层拒绝。只补齐该调用后，固定程序返回 `answer=42`、虚拟 UID 0 和 `4.19.0-gvisor`，见 `test-results/g22-prototype/px-g22-runsc-2c729263a021.json`。前两次失败和清理记录均保留。此原型只能证明单次 CPU 启动，不代表下方组件的故障矩阵或 GPU 成功。

新实现将运行时归档及源码放入独立构建上下文，核对完整归档摘要和不可变镜像身份；原普通监督器与新增运行配置使用不同的预约身份范围。guest 采用直接 OCI runsc、只读根、空能力和断网；外层仍为 UID 65532。会话复用原有有界协议；所有探针输入均为固定代码和随机测试挑战，不接入学员 API。

## 最终验证

| 检查 | 实际结果与记录 |
| --- | --- |
| 容器内 runsc | 五组固定 CPU 检查通过：隔离身份、输出超限、超时、临时存储耗尽、附着客户端退出；后四组均成功启动新 guest 恢复，共确认九次内部 OCI 清理。记录：`test-results/px-container-runtime-d09d6d8b17c8/result.json` |
| 外层监督 | 固定控制器退出后容器自行退出；硬期限到达后以 124 退出。两组均先观察停止，再清理；连同常规检查，三组已记录的宿主进程身份全部消失，三个自有外层容器及镜像标签已移除 |
| 原监督器回归 | 七类真实 CPU 场景通过：忙循环期限、主进程退出、创建者退出、接管、同名替换、标签不符、过期启动。八个自有容器及镜像标签已移除；实际 CPU 节流计数由 0 增至 20。记录：`test-results/px-container-supervision-db6539f62fe1/result.json` |
| 组件与发布兼容 | 146 项通过，无跳过；覆盖两种监督器、受控命令、设备锁及发布检查。记录：`test-results/g22-components-final.log` 和同名 XML |
| 静态与内容 | Ruff 检查通过，525 个 Python 文件格式检查通过，无待生成迁移；现有 50 节点、26 题静态校验通过，未执行参考代码。记录：`test-results/g22-{ruff,format,migrations,content}.log` |
| 文档与清理复核 | 139 份 Markdown 的 761 个本地链接、改动文档的 38 个锚点通过；新增 CLI 参数与文档一致。最终源码匹配，六份正式重放报告涉及的 15 个测试容器与镜像标签再次确认不存在，原型标签也已移除；现有三个站点服务仍健康。记录：`test-results/g22-document-links.json`、`test-results/g22-audit.json` |

真实 guest 的 UID 为 0、有效能力全零，根目录不可写，外层私有文件不可见，外层回环监听不可达，无 GPU 设备节点；环境变量名称恰为 `HOME`、`LANG`、`PATH`、`PYTHONDONTWRITEBYTECODE`。固定输出带有随机测试挑战的摘要。附着客户端退出后，runsc 报告 guest 仍为 `running`；清理必须确认 OCI 状态及私有进程身份消失，不能把客户端结束当作 guest 结束。PID 1 同时回收孤儿辅助进程，保留受监督主进程的退出状态。

本批最终运行时镜像为 `sha256:f7897396e78fa9f34d7f100e4f858d1d9ba0acf5bf889e0534eef7156566a9f5`，源码契约为 `b0f20a121f9729a7707d18a9b68291eb1553bd9ac45ed92900c9aa63d2b21971`。普通监督器回归镜像为 `sha256:75b6d1c51bd86e09009a0c4b27f4c967263fd4ac494a66c8b3c7d62d9b24d76d`。报告逐文件记录实际源码摘要；两次构建均使用固定 Python 镜像、空白 Docker 客户端配置和最小环境。

runsc 来自 `release-20261026.0` 完整归档，SHA-512 为 `26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a`；二进制及 sidecar 均来自核验后的私有副本。上游 seccomp 快照及 gVisor 许可说明保留在 `vendor/`，运行时二进制不提交到仓库。

## 原始失败与修复

初轮组件及发布检查为 143 项通过，镜像标签专项为 90 项通过，清理专项为 91 项通过；最终 146 项及上述真实重放覆盖最终实现。原始失败报告保留，各轮已创建的自有容器和镜像标签均已清理：

- 第一轮在启动前发现镜像继承的运行配置标签未被原完整标签核对接纳；容器未执行。修正为按各自配置核对完整标签，并增加拒绝标签篡改的检查。记录：`test-results/px-container-runtime-9c3c080b4f4b/result.json`。
- 第二轮未收到固定 guest 的就绪报告，清理前没有取得内部原因。随后补齐有界错误记录。记录：`test-results/px-container-runtime-281b2f4fda69/result.json`。
- 第三轮保留了内部 `TypeError: 'NoneType' object is not iterable`：真实 runsc 将空列表编码为 JSON `null`。显式接受空列表后，仍独立核对残留进程，并让 PID 1 回收孤儿子进程。记录：`test-results/px-container-runtime-102c08af7cb4/result.json`。
- 第四轮固定 guest 已启动，但身份断言失败；当时记录不足以定位具体字段。随后为 guest 明确配置 `HOME=/tmp`、`LANG=C.UTF-8`，并在失败时保留完整固定探针身份。第五轮才取得上面的全部最终结果。记录：`test-results/px-container-runtime-3ee8176dd510/result.json`。

## 验证边界

2 CPU、2 GiB、无 swap、512 任务是外层整个监督容器的限额，包含控制器、runsc、gofer 和 Sentry；不能当作宿主路径的独立 0.5 CPU、512 MiB guest 限额，也不能由配置数值宣称所有资源故障已实测。正式接入需先定义版本化资源契约，并完成相应故障和总预算探针。

新代码尚未进入 GPU worker、可信通信、设备注入、启动准入或发布协调器。没有改变已有成绩、数据库、默认题库、当前 CPU/GPU 执行策略，也没有重建原有站点或重新执行完整 Compose/浏览器矩阵。后续仍需设备身份、持续占用监测、完整启动和恢复、真实 CUDA/内容、部署及升级验收，完整范围保留在[需求追踪](requirements.md)。
