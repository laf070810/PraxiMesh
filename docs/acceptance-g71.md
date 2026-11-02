# G71：当前默认 Compose 的完整重放

日期：2026-11-02。验证提交 `bd4a280`。本轮没有修改应用或评测实现；补齐 G68–G70 增量在默认自部署入口的实际交付证据，不查询、映射或执行 GPU。

## 验证缘由与范围

默认 Compose 上次完整实跑对应 [G67](acceptance-g67.md) 的 `02bd734`。`test-results/g71-compose-source-gap.json` 确认此后有十个随应用镜像交付的运行源码变化，包括本地宿主状态、控制容器、公开调用者、共用准备和桥接退出同步。因此 G70 的独立编译器、GPU worker 预检与静态双镜像构建不能替代当前 Web/CPU Compose 的完整检查。

按[默认 Compose 验收入口](deployment/compose.md)运行现有 `scripts/compose_acceptance.py`，使用私有 Docker 配置、独立项目和临时端口，原有服务不参与。页面、编辑器 bundle 和模型实现未变，本轮没有重跑可选 AI 或浏览器矩阵。没有导入旧 F 镜像、改写既有发布制品或部署到目标站点。

## 实际结果

`test-results/g71-compose.log`、`g71-compose-process.json` 与 `px-compose-20fff999c983/result.json` 记录进程正常结束、`passed=true`、`owned_resources_removed=true`：

- 镜像内首次完成 **129 项真实 WASI 隔离探针**；七种题型的正确 Submit、针对性错解与公开 Run 共 **九次 HTTP 提交**得到预期结果。
- Web 与 CPU worker 在首次启动、保留数据卷重建后均能计算当前 GPU 执行源码摘要 `a1e876cbef0bfa65b56fd82b567fc1e57275a3b85a0cbf1c7e9561bac8d3fba2`。该检查仅证明完整源码已随镜像交付，GPU 健康仍为不可用。
- worker 离线后，正式提交返回 503，题库和其他非评测页面仍可浏览。
- 独立端口监听 `0.0.0.0`；实际直接 IP 访问、同源草稿保存成功，跨源 POST 被拒绝。
- 删除并重建容器后，原账号、草稿、会话、密钥、正式成绩、自定义内容和撤回状态均保留，没有重导覆盖原目录。
- Web/worker 保持非 root、只读根；Web 不包含执行运行时或 Docker socket，CPU worker 无网络。由 HTTP 下载的编辑器资源逐字节匹配当前 manifest，WASM MIME 类型正确。

重建 worker 再次执行完整探针，原日志 `px-compose-20fff999c983/34.log` 有 **129 项 PASS** 和完整结束标记。两轮使用镜像内的新凭证，没有用旧 receipt 或宿主缓存代替自检。CPU 策略摘要为 `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9`，解释器 bundle 为 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`。

## 清理和未完成门槛

`g71-resources.json` 独立复核当前源码与报告一致；测试项目的容器、网络和卷全部消失，私有 Docker 配置已清理。原 Web、worker、tutor 的容器 ID、镜像和启动时间未变，G70 保留的编译器及本地控制/任务镜像仍在。

本机 Compose 为 5.6.1；核对其[镜像删除调用](https://github.com/docker/compose/blob/v5.6.1/pkg/compose/down.go)及[对应 SDK](https://github.com/moby/moby/blob/client/v0.6.1/client/image_remove.go)，默认删除选项设置 `noprune`，不需要将 G70 的 CLI 参数修复套用到 Compose 子命令。本轮没有改动清理实现。

`g71-ci-status.json` 的公开 API 查询确认远端仍为 `d2c6560`：应用工作流成功、WASI 工作流失败，日志下载接口仍返回 403。新本地提交未推送，本轮不构成托管复验，也不能据此判断旧失败原因。后续步骤统一见 [CI 指南](deployment/ci.md)。

真实 GPU 全路径及部署升级、旧 F 应用整站升级、目标站点与维护者外部事项仍按[需求追踪](requirements.md)保留；完整 PRD 尚未完成。
