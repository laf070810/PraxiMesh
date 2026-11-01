# G61：无头浏览器绘制环境与发布包复验

日期：2026-11-02。对应 PRD R-09；起始提交 `1699f8d`。继续不访问 GPU、不改动既有服务，不推送或触发远端工作流。操作集中在 [CI 指南](deployment/ci.md)、[浏览器准备](deployment/validation.md#准备与运行)与[发布包验收](deployment/releases.md#重放镜像验收)。

## 远端状态与本地复现

公开 GitHub API 确认远端仍为 `d2c6560825211d60dd48c742e470ab188d69e612`；应用检查成功，WASI 作业 `41002028143` 在 `Import and exercise the packaged release` 失败。注释只有退出码 1；日志接口仍为 403，本机没有 GitHub API 认证。`real-wasi-acceptance` artifact 尚未过期，但未下载内容。证据在 `test-results/g61-ci-public-status.json` 和 `g61-ci-log-access.json`。未将其他工作流或本地结果当作此远端提交已通过。

使用 [G59](acceptance-g59.md) 的冻结 CPU 发布包重放同一入口，清单 SHA-256 为 `931ab92cf73512a5372e14fa4fc976934e57d79b8250369dd4aac81a4ddf1da1`。首次运行前 375 个运行源码文件与工作树匹配，完整归档及对应 WASI 摘要均核对；未加载旧 F 镜像或复用旧启动 receipt。

`g61-release.log` 和 `g61-release/` 保留原始失败：镜像内 **129 个真实 WASI 启动探针通过**，随后 `browser_smoke.py` 的首页截图在 30 秒超时，尚未进入浏览器提交验证。Web 正常返回页面、样式和脚本。`g61-release-resource-check.json` 确认临时容器/网络/卷全部清理，原三个服务的 ID、镜像和启动时间未变，新发布镜像仍保留。

## 触发条件与修复

相同缓存中的 Playwright 1.64.0 / Chromium 156.0.8078.4 可正常启动、加载 DOM 和字体，但最小的一行 HTML 也无法截图。原生 CDP 截图和动画帧回调同样不返回；前台切换、调整视口、关闭硬件加速、旧截图路径和独立下载的同版完整 Chromium 均未解决。相关原始记录分别在 `g61-home-probe/`、`g61-cpu-render-probe/`、`g61-legacy-render-probe/`、`g61-browser-protocol/`、`g61-full-chromium-probe/` 及同名前缀日志中。它们不证明应用页面有错，也不能反推远端旧作业失败原因。

对照实验只改变浏览器子进程环境：去掉显示连接变量或去掉 D-Bus 会话变量，均在一秒内生成真实 PNG；同时移除也通过。`g61-desktop-environment-probe/result.json` 记录两项对照，`g61-headless-environment.png` 已实际查看。触发因素是此环境继承的桌面会话组合，不是字体未加载或页面动画。

所有无头浏览器改用 `tests/browser_runtime.py`：对子进程移除 `DISPLAY`、`WAYLAND_DISPLAY`、`DBUS_SESSION_BUS_ADDRESS`，保留父进程环境、缓存位置及既有精确 TLS 参数。没有改写依赖、旧缓存、应用页面或截图，也没有采用上述无效开关。新的预检实际生成 640×480 PNG 后关闭浏览器，CI 在 WASI 构建前执行；Compose 浏览器模式与发布包入口复用同一预检。

发布入口在预检前清除自己的旧成功摘要。新增回归先复现渲染失败后遗留旧预检记录，修复后确认在任何归档/Docker 操作前结束，旧成功记录消失、原诊断日志保留。原始失败在 `g61-preflight-before.xml/.log`；这些控制模型不代替实际浏览器验证。

## 当前验证结果

- `g61-runtime-preflight.json`：在原显示与 D-Bus 环境仍存在的调用进程中完成真实截图，父进程环境完全不变。
- `g61-components.xml/.log`：**73 passed、0 skipped**，覆盖验收控制、组织夹具和发布包规则。
- `g61-browser-fixed.log`：完整源码浏览器退出码 0；编辑器、草稿、学习记录、社区审核/举报/申诉/撤回/导出、账号删除、英文和工程项目流程通过。`g61-browser-fixed/editor/layouts.json` 的中英 25 页、320/390/768/1440 四种宽度共 **200 组**通过，覆盖标题/正文边界、筛选控件和复选框对齐及横向溢出检查。
- `g61-gpu-render-run.json` 与 `px-gpu-results-c572304ae28f/result.json`：统一启动器的补充渲染通过，包含 24 组结果页、6 组关闭状态、2 组 CUDA 编辑器、6 组发现入口、38 组分享/榜单检查及跨账号拒绝。仅使用合成数据，没有 GPU 执行；一次性数据库已清理。实际查看了英文 390px 分享页和中文 CUDA 编辑器截图。
- 源码浏览器使用一次性 SQLite 和回环服务，评测关闭；结束后由原入口清理数据库及服务。全仓库 Ruff、格式检查通过。

## 冻结发布包复验

修复提交 `d3989925079504b9afb1334fce7700ae4b26be19` 的验收脚本已完整重放上述 G59 发布包，`test-results/g61-release-fixed/release-acceptance.json` 记录 **passed=true**。归档仍是原不可变制品；复验前纳入对比的 375 个归档源码文件中，372 个与当前工作树一致，只有三个宿主验收脚本不同（`browser_organizations.py`、`compose_acceptance.py`、`release_acceptance.py`）。应用和 worker 源码未改；宿主使用新的统一浏览器启动器。具体对比和原服务身份见 `g61-release-fixed-before.json`。

镜像内重新完成 **129 个真实 WASI 启动探针**，随后浏览器完成真实 Run、错误提交、Accepted、学习进度和账号删除。中英、工程项目、筛选，以及专业题型、博客、版本反馈、组织课程、规则辅导、挑战、类/API 流程全部通过；不同功能流程使用独立限流窗口，没有在流程内重置计数。准入超时关闭、真实队列排空与恢复，以及错误 WASI bundle 在 worker 启动前拒绝也通过。

本次使用私有 Docker 网络、回环 HTTP 和合成 PostgreSQL 数据库，保留 owner/Web/worker 分权、非 root 只读容器、Web 无执行运行时、无 Docker socket/宿主绑定和 worker 内部网络。AI 关闭；不将规则辅导当作模型验证，也不将此次 HTTP 复验当作新的 HTTPS 或 Compose 全栈验收。

`g61-release-fixed-resource-check.json` 确认退出码 0、临时容器/网络/卷全部清理、原三个服务 ID/镜像/启动时间未变、发布镜像保留。首次失败目录和日志仍保留；独立下载用于诊断的浏览器缓存已清理，原缓存未变，见 `g61-browser-cache-cleanup.json`。

此次没有修改默认题库、应用 schema 或 CPU/GPU 执行策略，没有访问 GPU、加载旧 F 镜像或推送。托管 CI、旧 F 应用跨版本升级和真实 GPU 的门槛继续保留；远端旧失败是否同因仍无日志证据。
