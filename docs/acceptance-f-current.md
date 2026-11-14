# 旧 F 应用到当前 CPU 版本的整站升级

日期：2026-11-15（Asia/Shanghai）。对应 PRD R-09、AC-07。维护者已明确允许导入旧 F 镜像并执行临时站点升级；操作入口在[生产部署指南](deployment/production.md#f-current-upgrade)。

**实际运行旧 F 应用、保留其数据并升级到当前 CPU 版本的验收已通过。** 本次使用独立 PostgreSQL、回环 HTTPS、真实 WASI worker 和合成账号，未更新已有 Compose 站点。它补齐了[此前准备记录](acceptance-upgrade-preparation.md)中尚未执行的跨版本应用验证。

## 固定输入

验收调用者为干净提交 `a8a3457b93e3a0159f882d1ca78e921ff238feb2`。发布包沿用原归档，未重新打包或修改历史迁移。

| 输入 | 目录与身份 |
| --- | --- |
| 旧 F | `var/releases/2026-10-24-f-candidate-1/`；manifest SHA-256 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` |
| 当前 CPU | `var/releases/2026-11-13-cpu-upgrade-target-4/`；manifest SHA-256 `767551ab285abe431a5e0ca692d0a34631a122227dd80510ca2a97e24e4e0741` |
| 当前源码 | `0b9a69c6c8d7a700c62a3c9e4e579a129616de14`；981 个文件及镜像身份见[同源发布包](acceptance-current-releases.md) |
| WASI bundle | SHA-256 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d` |
| Caddy | 固定 2.11.7，官方归档经脚本校验；二进制 SHA-256 `678ade3bfc088749c81a681adc603333ee0bb023b6a6cfe3c0f58bef8ff854e9` |

使用 `scripts/deploy_acceptance.py --upgrade-only`，分别传入上述原包与新包、各自摘要及新证据目录。浏览器预检实际生成截图；整站命令退出码为 0，耗时 475.595 秒，报告为 `passed: true`、`phase: complete`，共两代应用。

## 实际验证范围

- 运行旧 F 应用后建立真实 CPU 提交历史及预约、账号、会话、草稿和私有学习数据，经数据库复制和新应用迁移比较保留结果；升级不会自动发布或覆盖目录。
- 实际新增 `core.0004_gpu_resources`、`core.0005_gpu_admission_environment` 和 `showcase.0002_shared_metric_scope`；旧迁移保持原字节。GPU 设备表为空。
- 核对新资源表与学习/社区表的实际角色权限。Web 对设备表仅可读；worker 可读、插入和更新设备记录，不能删除或取得额外表权限。
- 升级后验证真实评测、会话、令牌、请求幂等性、私有导出，以及 HTTPS 浏览器中的版本题目反馈。原分享继续使用 `sequential-v1` 字段范围。
- 验证保留账号的 Web 角色删除与级联。备份恢复比较覆盖 58 张表、33 个序列和 550 行数据。

本次成功升级不包含跨版本失败迁移回滚或 worker 自动重启专项；当前版本的完整故障恢复矩阵另见 [G59](acceptance-g59.md)。回环证书不证明公网证书或系统信任。本次升级未访问 GPU，不证明 CUDA 执行、GPU 队列或 GPU 发布升级成功。

## 清理与原站点

验收报告确认清理通过，无残留自有资源、数据库命名卷已删除、本地进程均终止，独立 Docker 客户端目录也已删除。再次只读核对原 Web、CPU worker、tutor 的完整容器 ID、镜像、启动时间和健康状态，均与本轮开始前一致。旧 F 的两份不可变镜像缓存已恢复，原发布归档保留。

原始私有证据位于忽略目录 `test-results/f-current-authorized-2026-11-15/`：

- `preflight.json`、`browser-preflight.json`：授权范围、固定输入和实际截图预检。
- `upgrade-1-process.json`、`upgrade-1.log`：原命令、进程、终态及耗时。
- `upgrade-1/deployment-acceptance.json`：数据、权限、迁移、浏览器及清理结果。
- `upgrade-resource-observation.json`：原服务和旧镜像缓存的最终核对。

同日维护者也重新授权使用 GPU 0；设备身份和初步完整 SMI 空列表观察单独保留，不并入本页 CPU 升级通过结论。GPU 硬件、专用部署 runner、目标站点及公开运营政策仍按[需求追踪](requirements.md)分别完成。
