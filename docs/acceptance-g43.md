# G43：GPU 发布协调的原节点绑定

日期：2026-11-01。状态：节点绑定与无设备中断验收通过，CPU 协调器的完整恢复重放仍在进行；GPU 代际激活与真实升级未完成。设计见 [RFC-022](rfcs/022-gpu-release-coordination.md)、[ADR-059](adr/059-gpu-deployment-attachment.md)，操作集中在[节点绑定指南](deployment/container-gpu-worker.md#coordinator-binding)。本轮没有读取或映射 GPU。

## 已实现与验证

`attach-gpu` 允许原站点只增加明确的 GPU 配置，并在原设备锁下核对操作者、目录/socket/锁 inode、daemon、完整 v4 挂载清单及安装身份。先写部署侧意图，再认领原状态，最后确认摘要；重复调用不改写既有所有权记录。已有 worker、活动/历史任务、其他认领或未知临时文件均拒绝接管。未确认绑定在数据库连接前阻止发布和恢复；独立配置工具也不能写入已绑定节点。

`test-results/g43-attachment-real/result.json` 的九项真实无设备检查通过：首次绑定和幂等重试、跨进程原锁竞争、复制协调器拒绝、意图/临时文件/所有权记录/确认四处实际 SIGKILL 后按原意图继续，以及旧 worker 配置和未知活动记录拒绝。原锁和挂载字节保留，私有测试状态清理完成。记录中的 Docker 调用只包含实际 `info`；没有启动容器、数据库或代理。设备元数据与已初始化站点记录是明确的合成夹具，不能作为真实 GPU 身份、硬件或部署成功的证据。

276 项绑定、旧 CPU 协调器、配置、发布格式和代理检查通过，见 `test-results/g43-binding-final.xml`。后续补充的独立配置所有权检查通过，见 `g43-owner-config.xml`；新旧编辑器的显式验收契约、绑定与私有配置专项 109 项通过，见 `g43-compatibility.xml`。这些集合有重叠，不累计成独立用例数。

新准备模式只增加 `--prepare-only` 参数分支，真实设备初始化未执行。参数冲突或不完整设置在设备访问前拒绝，正常 worker 配置也先校验数据库 URL；正向委派使用替身验证，不宣称完成元数据初始化验收。

## 原始失败与 CPU 回归

第一轮组件失败来自新增夹具缺少 Caddy 可执行文件和旧 CLI 替身不接受新的锁参数；补齐夹具后 120 项通过。原结果在 `g43-binding-first.log`、`.xml` 与 `g43-binding-second.log`。另一命令把实际 `test_gpu_release.py` 误写为不存在的路径，没有运行用例，日志保留在 `g43-binding-complete.log`。

使用独立可信 F 发布包 `896e1c1b605025376ed30786ea4e79d3fde008c97e7d114bf91febd86cc62482` 和固定 Caddy 2.11.7 重放当前 CPU 协调器。首轮真实 WASI 启动及应用安装通过，浏览器脚本随后要求旧 F 包不存在的 CodeMirror 而失败；不是 GPU 或部署成功。失败及完整清理记录在 `test-results/g43-cpu-coordinator/`。

已让浏览器契约由实际当前代的已验证源码清单决定：旧包明确使用 textarea，新包仍强制 CodeMirror，没有“加载失败就算旧版”的自动回退。截图保存到本次演练目录。完整重放进程与结果位于 `test-results/g43-cpu-coordinator-fixed/`；待其结束再记录升级、曝光前后恢复与数据保留结论，当前不计作通过。

## 继续完成的范围

当前 `apply` 仍在服务或数据库变更前拒绝四镜像 GPU 激活。下一步是把每代私有 worker 配置与对应数据库持久绑定，接通原任务清理、排空、候选完整启动、GPU 正确/错误队列及曝光前后恢复；这些不以本次文件/daemon 检查替代。真实 GPU 设备读取、映射、完整启动和升级验收继续等待明确授权及独占条件，完整 PRD 目标保持未完成。
