# G43：GPU 发布协调的原节点绑定

日期：2026-11-01。状态：节点绑定与无设备中断验收通过，CPU 完整演练在切换后恢复失败，定向诊断正在进行；GPU 代际激活与真实升级未完成。设计见 [RFC-022](rfcs/022-gpu-release-coordination.md)、[ADR-059](adr/059-gpu-deployment-attachment.md)，操作集中在[节点绑定指南](deployment/container-gpu-worker.md#coordinator-binding)。本轮没有读取或映射 GPU。

## 已实现与验证

`attach-gpu` 允许原站点只增加明确的 GPU 配置，并在原设备锁下核对操作者、目录/socket/锁 inode、daemon、完整 v4 挂载清单及安装身份。先写部署侧意图，再认领原状态，最后确认摘要；重复调用不改写既有所有权记录。已有 worker、活动/历史任务、其他认领或未知临时文件均拒绝接管。未确认绑定在数据库连接前阻止发布和恢复；独立配置工具也不能写入已绑定节点。

`test-results/g43-attachment-real/result.json` 的九项真实无设备检查通过：首次绑定和幂等重试、跨进程原锁竞争、复制协调器拒绝、意图/临时文件/所有权记录/确认四处实际 SIGKILL 后按原意图继续，以及旧 worker 配置和未知活动记录拒绝。原锁和挂载字节保留，私有测试状态清理完成。记录中的 Docker 调用只包含实际 `info`；没有启动容器、数据库或代理。设备元数据与已初始化站点记录是明确的合成夹具，不能作为真实 GPU 身份、硬件或部署成功的证据。

276 项绑定、旧 CPU 协调器、配置、发布格式和代理检查通过，见 `test-results/g43-binding-final.xml`。后续补充的独立配置所有权检查通过，见 `g43-owner-config.xml`；新旧编辑器的显式验收契约、绑定与私有配置专项 109 项通过，见 `g43-compatibility.xml`。社区兼容修复后，当前八个相关模块的 304 项检查与验收工具的 23 项检查分别通过，均无跳过，见 `g43-components-final.xml`、`g43-harness.xml`；此前模块名误写的命令未运行用例，保留在 `g43-components-current.log`。前后检查有重叠，不累计成独立用例数。

新准备模式只增加 `--prepare-only` 参数分支，真实设备初始化未执行。参数冲突或不完整设置在设备访问前拒绝，正常 worker 配置也先校验数据库 URL；正向委派使用替身验证，不宣称完成元数据初始化验收。

## 原始失败与 CPU 回归

第一轮组件失败来自新增夹具缺少 Caddy 可执行文件和旧 CLI 替身不接受新的锁参数；补齐夹具后 120 项通过。原结果在 `g43-binding-first.log`、`.xml` 与 `g43-binding-second.log`。另一命令把实际 `test_gpu_release.py` 误写为不存在的路径，没有运行用例，日志保留在 `g43-binding-complete.log`。

使用独立可信 F 发布包 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` 和固定 Caddy 2.11.7 重放当前 CPU 协调器。首轮真实 WASI 启动及应用安装通过，浏览器脚本随后要求旧 F 包不存在的 CodeMirror 而失败；不是 GPU 或部署成功。失败及完整清理记录在 `test-results/g43-cpu-coordinator/`。

第二轮的真实 HTTPS Run、错误 Submit、正确 Submit 和学习流程通过，社区浏览器仍使用独立的 CodeMirror 选择器，随后在保存私有草稿前失败。日志和完整资源清理记录在 `test-results/g43-cpu-coordinator-fixed/`；社区详细 JSON 当时仍写入临时目录，已随清理删除，不能冒称留存。

已让两个流程复用同一编辑器操作，并由实际当前代的已验证源码清单决定契约：旧包明确使用 textarea，新包仍强制 CodeMirror，没有“加载失败就算旧版”的自动回退。社区报告、截图及源码浏览器日志均写入显式验收目录。当前源码的编辑器、25 个页面 × 两种语言 × 四种宽度的 200 组布局，以及学习、社区审核、英文和项目浏览器流程通过，记录在 `test-results/g43-current-browser/` 与同名 `.log`，临时数据库和服务已清理；此源码检查未运行评测或使用 GPU。

第三轮 `test-results/g43-cpu-coordinator-final/` 通过真实 HTTPS 评测和社区流程、代理 SIGKILL 后恢复、正常代际替换，以及切换前 SIGKILL 后返回上一代；备份核对 58 张表、33 个序列、531 行。第四代实际曝光并产生写入后，`recover` 在服务停止后的流程返回通用 `DeploymentError`，原操作记录只有 `recovering_current`、没有失败详情。随后只读查询故障窗口内的用户 Docker 服务日志，精确匹配到本次 Web 容器：RootlessKit 重启绑定 `127.0.0.1:32830` 时返回 `bind: address already in use`；四条相关记录保存在 `owned-web-daemon-failure.json`。端口占用方未确认，这轮不计作完整恢复通过。最终 `cleanup.passed=true`，本次容器、网络、数据库卷和本地进程已清理；未修改共享 daemon 或其他服务。

已补齐恢复失败阶段记录、所属容器启动/停止的脱敏私有诊断，以及验收清理前对所指诊断文件的有界保留。新增独立单代 `--recovery-only` 入口，范围与命令集中在[恢复复验](deployment/production.md#复验)。首次组件运行的三个旧夹具缺少新参数，原失败见 `g43-recovery-diagnostics.xml`；补齐后 123 项通过，1 项实际 PostgreSQL 权限检查因本轮使用 SQLite 跳过。扩大后的当前相关集合为 374 项通过、同一项跳过，见 `g43-components-diagnostic.xml`；当前协调器的九项真实无设备绑定复验通过，见 `g43-attachment-current/result.json`。单代真实复现位于 `g43-recovery-diagnostic/`，尚未完成；新增诊断本身不作为原恢复故障已修复的证据。

## 继续完成的范围

当前 `apply` 仍在服务或数据库变更前拒绝四镜像 GPU 激活。下一步是把每代私有 worker 配置与对应数据库持久绑定，接通原任务清理、排空、候选完整启动、GPU 正确/错误队列及曝光前后恢复；这些不以本次文件/daemon 检查替代。真实 GPU 设备读取、映射、完整启动和升级验收继续等待明确授权及独占条件，完整 PRD 目标保持未完成。
