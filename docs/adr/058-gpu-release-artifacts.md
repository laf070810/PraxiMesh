# ADR-058：同一冻结源码的四镜像 GPU 发布制品

日期：2026-11-01。状态：已采纳，制品、导入与非 GPU 应用验收完成。设计见 [RFC-021](../rfcs/021-gpu-container-supervision.md#gpu-release-artifact)，操作统一在[发布制品指南](../deployment/releases.md#gpu-release)，结果见 [G42](../acceptance-g42.md)。

GPU 发布使用可选 format 2，在原 `release.json`、`source.tar.gz`、`images.tar` 内绑定 Web、CPU worker、GPU worker 和任务镜像。默认仍生成 format 1；新读取器继续验证旧制品，不增加旧包必需文件。构建只执行本机审阅源码的一次冻结副本，不将已下载归档中的代码当作构建器。

保存每个 GPU 契约的有序源码范围，从归档字节重新计算；任务契约同时绑定固定驱动清单。完整管理制品须与任务驱动版本一致。镜像归档独立验证实际配置、层内容、角色、入口和契约，不能依赖 daemon 缓存，也不能通过可变标签覆盖其他服务。GPU worker 的 UID 0 仍只用于原 rootless 映射，Web/CPU worker 与任务保持非 root。

制品不包含数据库、设备 UUID、部署状态、locks、journal 或 receipt；构建、校验和导入不查询 GPU、不执行 CUDA、不发布就绪。原无设备验收可直接使用已核验发布镜像，并保留默认关闭、权限拒绝、源码改写和旧控制契约拒绝检查。

本批只交付升级所需的制品层。未接入 GPU 生命周期的协调器拒绝 format 2 的激活，避免只运行两个 CPU 服务后误报完整发布成功。后续还须完成共同排空、设备控制器停止、原私有状态保留、候选数据库完整启动及开放前后恢复；真实硬件结果和部署升级仍单独验收。
