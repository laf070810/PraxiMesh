# G59：当前 CPU 发布制品与完整恢复演练

日期：2026-11-02。对应 PRD R-09、AC-07。当前 CPU 制品的完整发布恢复矩阵通过。使用独立临时 PostgreSQL、回环 HTTPS、真实 WASI worker 和合成账号；没有访问 GPU 或改动现有 Compose 服务。构建与运行入口分别见[发布制品](deployment/releases.md)和[完整发布演练](deployment/production.md#full-deployment-validation)。

## 真实失败与修正

首次制品在私有 `umask 077` 下构建，源码文件内容与提交一致，但嵌套目录以 0700 进入镜像。非 root 用户无法发现 `content_import`，首次安装停止；管理命令、迁移和静态资源目录的实际模式记录在 `test-results/g59-source-directory-modes.json`。原失败、受限控制诊断和自有资源清理结果保留在 `g59-cpu-deployment/`，原发布目录不改写。

提交 `5a4ebb4` 在白名单快照内固定源码目录 0755、文件 0644，保留外层私有工作区与原始源码权限，也不修改调用者的 umask。`g59-directory-permissions-before.xml/.log` 的两项回归先失败；修正后 `g59-release-components.xml/.log` 的 **52 项检查通过**，没有跳过，覆盖制品读取、旧格式兼容、快照与失败迁移制品。Ruff、格式、链接和差异检查通过；CPU/GPU、控制器和 worker 执行源码契约均未变。

新镜像的实际目录模式在 `g59-source-directory-modes-2.json` 确认为 0755。第二次演练完成首次安装和全部 **129 个真实隔离探针**，随后浏览器因默认缓存路径缺少 Chromium 而未启动；日志与清理记录保留在 `g59-cpu-deployment-2/`。配置已有 `PLAYWRIGHT_BROWSERS_PATH` 后，实际启动与关闭检查通过，见 `g59-browser-preflight.json`；没有安装新依赖或同步服务环境。

## 固定制品

当前 CPU 制品为 `var/releases/2026-11-02-g59-cpu-2`，912 个冻结文件逐一与 `5a4ebb4` 比较一致。失败迁移制品仅用于自有测试数据库，在该快照上追加一项先创建表再抛出固定错误的原子迁移；不得部署到实际站点。

| 身份 | SHA-256 |
| --- | --- |
| 当前 CPU 发布清单 | `931ab92cf73512a5372e14fa4fc976934e57d79b8250369dd4aac81a4ddf1da1` |
| 冻结源码 | `6d57cca00e0deb357f9306371953f2d86272e018fcfb4a3ffed991f2f8e1efb6` |
| Web 镜像 | `f9b8448ffe5f78c72262084a3c8846d108bf03571ca050e173dbc8fa796002a0` |
| CPU worker 镜像 | `1e776028fecdb63c0fa625228bbc1f72836060866e451b0e0f323231caf2f5cd` |
| 失败迁移发布清单 | `b0af7a4677362506ec6a8c1821b381fa25446b1e895a0483172131938889188e` |
| WASI bundle | `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d` |
| CPU 策略 | `0693e991c294e36f3479e7007201919578617c1180becd2a0ebba2b3ffd318b9` |

构建日志及源码比较在 `test-results/g59-cpu-build-2.log`、`g59-cpu-failure-build-2.log`、`g59-source-check-2.json`。两个新包均已按完整归档、镜像层与角色校验；本轮没有恢复旧 F 镜像缓存。旧 F→当前 schema 升级与本次同版本的代际替换分别验收，不能相互替代。

## 完整矩阵

第三次演练退出码为 0，`test-results/g59-cpu-deployment-3/deployment-acceptance.json` 的 `passed` 和 `cleanup.passed` 均为 true。覆盖五次代际尝试，没有编辑部署 journal 或伪造 verdict：

| 场景 | 实际结果 |
| --- | --- |
| 首次安装与浏览器 | 129 个真实 WASI 探针；HTTPS Run、错误 Submit、Accepted、私有学习记录、移动页面、账号删除及社区浏览器流程通过 |
| 代理 SIGKILL | 从持久活动配置恢复，处理遗留管理 socket，原用户数据不变 |
| 正常代际替换 | 实际备份/恢复比较 59 张表、539 行、33 个序列；账号、草稿和成绩保留，后续新账号序列正常 |
| 原子迁移故障 | 确认执行真实 DDL 后报错；故障表及迁移记录不存在，候选未曝光；回退后的数据和正确提交通过 |
| 曝光前 SIGKILL | 恢复上一代，原数据不变，未完成候选的 Web/worker 均停止 |
| 曝光后 SIGKILL | 恢复同一个已曝光数据库，新写入保留，已删除账号不复活 |
| worker 自动重启 | 实际终止控制器，Docker 重启次数增至 1；在新启动尚未就绪时进入恢复，最终保留原库和用户，并完成新的 HTTPS Accepted |

各次 worker 启动重新执行探针，重复日志不累加为独立测试数量。只有迁移失败夹具发生 schema 变化；成功代际使用同一个版本，`schema_changing_upgrade_tested` 为 false。该结果补齐 [G43](acceptance-g43.md) 后的当前 CPU 恢复矩阵，配合 [G58](acceptance-g58.md) 的固定占端口故障证明修复范围；不证明旧 F→当前 schema 升级、GPU、托管 CI、公有证书或目标主机长期运维完成。

## 独立收尾核对

`test-results/g59-resource-check.json` 确认三次尝试的临时容器、网络和卷均无遗留，测试子进程全部结束；现有 Web、worker、tutor 的完整容器 ID、镜像 ID 和启动时间与事前记录一致。新发布镜像保留用于复验。543 个 Python、JavaScript、CSS、Wasm 与译文文件仍与冻结制品一致；本轮后续改动仅为文档。

后续 [G60](acceptance-g60.md)使用与冻结 F 相同的历史迁移重建数据库，验证其真实复制、当前迁移、旧数据及序列保留和权限；未运行旧 F 应用，因此整站跨版本升级仍独立保留。
