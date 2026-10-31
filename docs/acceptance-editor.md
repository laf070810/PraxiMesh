# 浏览器编辑器与页面对齐验收

日期：2026-10-31。设计：[RFC-020](rfcs/020-browser-editor.md)、[ADR-021](adr/021-browser-editor.md)。使用和构建说明集中在 [编辑器指南](editor.md)。本轮在维护者要求的不占 GPU 范围内进行。

## 当前源码复查

2026-10-31 在 `main` 的 `207ed76` 界面基线上重放 `scripts/browser_acceptance.py --interface`，结果在 `test-results/g26-interface.log` 与 `test-results/g26-interface/editor/`。此次未修改页面或编辑器代码。Python/JavaScript 的高亮、补全、真实本地检查与格式化、快捷键、私有草稿及降级行为通过；没有执行评测或占用 GPU。

中英文、1440/768/390 像素的练习筛选、知识列表、学习目标、组织列表和做题页共 30 组布局通过，未发现横向溢出。检查了标题边距、同排筛选控件底边、复选框尺寸、实际点击与 Space 切换；并查看了桌面练习页及编辑器、平板知识页、手机英文目标选项页的实际截图，未发现本次检查范围内的新增错位。源码测试使用一次性数据库与回环服务，不表示已替换正在运行的站点镜像。

## 已验证的源码行为

- Python/JavaScript 编辑器加载、高亮、行号、本地检查、格式化、撤销、内置名称补全、查找、Tab/Shift+Tab 缩进、注释、Esc 后 Tab 离开编辑器、Ctrl+S 私有保存与重开；worker 离线时不开放运行。
- 使用真实 Ruff WASM 与 Prettier 浏览器解析器。格式化语法错误时保留原文；人工延迟到真实 formatter 的请求以重现并发输入，旧结果不会覆盖新代码。超出 20000 字符的输入被拒绝且已有代码保留。
- 无 JavaScript 时通过原表单保存；增强模块加载失败时文本框保留；阻断 WASM 后仍可编辑和保存。英文编辑器按钮、检查提示与私有保存反馈通过。
- 中英文的 1440、768、390 像素页面检查覆盖练习筛选、知识列表、学习目标、组织列表和含长代码的题目编辑页，共 30 组。原目标/组织页面标题横坐标为 0，已统一边距；筛选控件同排底边对齐，单选/复选独立宽度，手机题目标签移到正文下方。补充手机检查记录在 `test-results/editor-mobile-final.log`。
- 实际浏览器未出现脚本错误或 CSP 违规；动态样式 nonce 与其他页面的严格策略分别检查。未放宽任意内联样式、JavaScript eval 或图片来源。

128 项相关检查通过，范围为 `test_editor`、`test_web`、`test_localization`、`test_coaching`、`test_release`，记录在 `test-results/editor-regression.xml`。完整 `scripts/browser_acceptance.py` 的编辑器、学习/草稿、社区、中英与工程项目流程通过，记录在 `test-results/editor-browser-full.log`。Ruff 检查和格式检查通过；锁定依赖 `npm audit` 未发现已知漏洞。

实际发布源码快照为 554 文件、约 15.9 MB，包含编辑器资源与许可证，不包含 `node_modules`；既有发布清单验证通过。部署直接携带预构建资源，不要求 Node。

## 原始失败与修复

所有原始记录位于忽略的 `test-results/`：`editor-before/`、`editor-interface-*.log`、`editor/`、`editor-web-1.xml`、`editor-policy-i18n.xml`。初次运行缺少当前 Playwright 的 Chromium，安装至临时缓存后继续；继承的 SSH `DISPLAY` 导致无头动画帧停滞，已在空白页面复现，仅为测试子进程移除显示变量后恢复。未修改用户会话或主机显示配置。

浏览器发现 CodeMirror 核心状态包有重复实例，已统一锁定版本并增加构建检查；诊断图标的 data URI 与严格 CSP 冲突，改用 CSS 图形和下划线。Playwright 对富文本直接 `.fill()` 会让 Chromium 插入带 token 颜色的内联样式，专项改为真实的全选、删除和文本输入，未增加 CSP 例外。补全测试尊重编辑器防误触等待窗口，查找面板使用实际 Enter 键提交查询。初次译文检查发现两条新帮助文字仍标为 fuzzy，确认译文并重新编译后通过。

## 验证边界

首次完整 Compose 重放 `test-results/px-compose-f1ea82adee37/` 在镜像内通过 129 个隔离探针、编辑器（含真实 Ctrl+Enter）、学习/正式提交、英文项目与类/API 浏览器流程；辅导流程发现选项容器 class 被 Django 继承到每个 checkbox，网格样式将点击宽度压为零。已将网格规则限于 div 容器，并补充最小点击尺寸、实际勾选与 Space 切换检查。修正后的源码专项及 30 组布局通过，记录在 `test-results/editor-choices-final.log`。该轮未通过的整体结果和清理记录保留，不计为完整 Compose 成功。

最终完整 Compose 重放通过，记录在 `test-results/px-compose-f5b1318868df/result.json`（`passed=true`、`owned_resources_removed=true`）：

- 镜像内重新执行 129 个 WASI 隔离探针，七类评测的正确提交、针对性错解和公开 Run 共九个场景通过；worker 离线后关闭提交。
- 编辑器、学习/草稿/正式成绩、英文项目、类/API、学习目标与辅导五组浏览器通过。辅导使用真实 CPU 模型并经过明确输入预览，正式成绩保持不变；这验证调用和界面，不是对建议质量的认可。
- `0.0.0.0` 的独立测试端口通过直接 IP 访问、同源保存和跨站拒绝；容器删除重建后账号、私有草稿、会话、密钥、成绩、自定义内容与撤回状态保留。
- 从重建后的服务下载 `editor.js`、`worker.js`、`ruff.wasm`，字节均与已提交资源一致，记录在 `editor-assets.json`。编辑器代码为 `14313c9`，复选框修复为 `9666411`；静态资源摘要见 `static/editor/manifest.json`。从锁文件重新安装和构建也得到相同字节。

源码浏览器使用一次性 SQLite 数据库、合成账号及回环服务；Compose 使用自有临时项目、账号和卷。结束时均清理自有资源，未改动已有站点。38 项发布与编辑器检查在可选 `.gitattributes` 加入后重新通过，148 个本地文档链接已核对。没有进行 GPU 执行、公开部署、数据库 schema 变更或旧内容重新发布；完整 PRD 的外部门槛继续保留在 [需求追踪](requirements.md)。
