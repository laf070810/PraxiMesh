# G44：无状态 Web 的持久重建与恢复

日期：2026-11-01。本轮完成实现及组件验证；后续 [G58](acceptance-g58.md)、[G59](acceptance-g59.md) 已补齐真实容器专项及当前 CPU 发布恢复矩阵。对应 PRD R-09、AC-07；[设计](rfcs/023-web-container-recovery.md)、[决策](adr/060-web-container-recovery.md)、[集中操作入口](deployment/production.md#web-recovery-check)。

## 实现范围

`scripts/deploy_web.py` 接入曝光前恢复上一代和曝光后保留当前代数据库的两条路径。实际代理须处于维护状态，Web 须已停止；校验完整运行契约和无持久挂载后保存意图，仅按原完整 ID 移除，再创建并持久保存新 ID 后启动。失败继续保留原数据库及维护状态。服务停止、启动和读取实际端口也核对已保存的重建身份。

`scripts/deploy_web_acceptance.py` 提供固定 CPU HTTP 服务、独占测试端口冲突、四处真实 SIGKILL 与同名替换检查，使用合成的协调器/代理元数据。它不使用数据库、学员代码或 GPU，不能替代完整发布验证。真实演练仍须使用 `deploy_acceptance.py` 的四代替换、曝光前后中断、写入/删除保留和 worker 自动重启矩阵；原不可变 F 制品不改写。

## 已执行的检查

- `test-results/g44-web-components-initial.xml`：97 项通过，包括十处重建中断、完整 ID 保护、未知环境/挂载/权限拒绝、运行但无映射不确认、失败启动不在同一容器重试，以及原部署控制流。
- `test-results/g44-components.xml`：13 个相关模块共 419 项通过，1 项 PostgreSQL 专项跳过；此结果不与前一轮相加。跳过项需要实际 PostgreSQL 的角色/FK 检查，本轮未宣称其通过。
- `test-results/g44-fixture-components.xml`：2 项独立的夹具清理回归通过，模拟实际发现的 Docker 默认父镜像清理行为，检查准备失败仍保留基镜像，以及缓存缺失时不创建或删除资源。这不是实际 Docker 恢复结果。
- 修改的 Python 文件通过 Ruff 检查及格式检查。数据库结构、运行角色权限和评测策略未改变。

## 真实检查的失败和当前缺口

以下失败均发生在测试夹具准备阶段，尚未验证新重建逻辑的真实端口恢复：

1. `g44-web-container-initial/`、`g44-web-container-build-diagnostic/`：构建器将 `FROM sha256:...` 解释为仓库引用，访问仓库时得到认证失败。改为从已存在的固定镜像创建从未启动的临时容器，只在本地派生固定 HTTP 命令的测试镜像，不修改登录配置。
2. `g44-web-container-local/`：合成代理夹具错误调用了需要真实 Caddy 配置的构造器；已改为明确的组件夹具，不改变实际部署代理检查。
3. 清理派生测试镜像时，Docker 默认连带删除了未打标签的原 F Web 镜像缓存。清理已改为 `image rm --no-prune`，并增加原基镜像保留断言。`g44-web-container-fixture/` 记录原缓存缺失，不能计为通过。

这些尝试的自有测试容器、网络和派生镜像已清理。只读记录 `test-results/g44-image-cache-scope.json` 确认原发布包摘要与两个无标签镜像，现有三个 Compose 服务使用不同镜像且启动时间未变化。原发布归档保留完整；恢复测试缓存的操作被自动审批阻止，正在等待维护者明确许可。未重启现有服务、修改 daemon 配置或接触 GPU。

本轮尚不能关闭 [G43](acceptance-g43.md) 的端口恢复缺口，也不代表 GPU 发布、硬件、托管 runner 或完整 PRD 已经完成。恢复测试缓存后须先通过实际端口/中断检查，再串行完成真实 PostgreSQL/HTTPS/WASI 发布矩阵。

## 后续实测

2026-11-02 的 [G58](acceptance-g58.md) 改用本账号已有的 Compose Web 不可变镜像 ID，通过六项实际 Docker/HTTP 检查，包括四处真实 SIGKILL；原服务及基镜像均保留。没有导入旧 F 镜像，先前的缓存恢复审批限制仍保留。端口专项缺口已补齐，完整 PostgreSQL/HTTPS/WASI 发布矩阵仍待完成；本页原始失败记录不变。

同日 [G59](acceptance-g59.md) 新建当前 CPU 制品，在真实 PostgreSQL/HTTPS/WASI 上通过五次代际尝试、迁移失败、曝光前后中断和 worker 自动重启恢复。当前版本的完整矩阵缺口已补齐，旧 F→当前 schema 升级仍独立待验收。
