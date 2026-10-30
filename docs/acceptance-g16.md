# G16：GPU 持久成绩与私有反馈

2026-10-31。实现范围见 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#持久成绩与学习证据)与 [ADR-032](adr/032-gpu-persisted-evidence.md)，操作入口统一在 [GPU 部署指南](deployment/gpu.md#gpu-results)。本轮遵循暂不占 GPU 的安排，GPU 记录均为一次性数据库中的合成夹具。

## 已实现

GPU 正式 Submit 通过已结束状态、执行时间顺序、已消耗的整卡预约、任务租约身份，以及结果和环境指纹检查后，接入现有学习、课程与挑战证据。公开 Run 只提供反馈，不形成正式完成；平台故障、不完整记录和他人提交均不计入。课程和挑战继续绑定指定题目版本，不能用新版本成绩替代旧版本要求。

历史记录按不可变会话配置注册表和保存的环境摘要验证，不要求旧执行摘要等于当前工作树；新会话语义必须追加版本。此校验用于可信 worker 已保存记录的一致性，不是上传 JSON 成绩的认证入口。GPU 成绩不进入既有 CPU 用例量榜单。

运行时 IPC 和持久反馈共用严格校验，拒绝未知字段、隐藏用例名、矛盾通过数、缺失编译记录、零采样或损坏中位数。全部正确性用例通过后，重复批次仍可能失败；这种合法失败只形成尝试，不显示性能分数。页面分别标注编译初始化及控制器批次往返中位数，明确通信和同步成本；GPU 预约显示整卡秒数。

本人结果页、API 和账户导出只使用校验后的反馈，其他账号无法查看。新增英文字符串及编译目录同步维护，修正预约说明各句之间缺少空格的问题。

## 组件与数据库

- `test-results/g16-evidence-initial.log/xml`：47 passed、1 failed，英文新增文案尚未编译；更新 `.mo` 后通过。
- `g16-result-contract-initial.log/xml`：六项失败复现矛盾公开通过数、接受成功但没有正编译耗时，以及零采样仍被 IPC 接受。共用校验修正后重放通过。
- `g16-evidence-controller.log/xml`：201 passed、2 failed。旧适配器成功夹具缺少编译耗时，另一测试依赖错误文字中的 `positive`；补齐真实成功格式并保留清晰错误分类，未放宽验证。
- `g16-component-final.log/xml`：356 passed、3 skipped，覆盖持久证据、运行适配器、控制协议、固定检查、会话、专业评测、Agent、类/API、翻译和 Web。三个跳过项为需要实际 WASI 的链路，随后全部单独重放，见下节。
- `g16-postgres-final.log/xml`：161 passed、无跳过。实际 PostgreSQL 重放证据、worker、完整启动、就绪事务、执行结算、控制与设备预约；包括领取/结算锁、心跳竞争和旧预约边界。GPU/服务运行方法使用明确测试替身，临时 PostgreSQL 容器及卷已清理。

上述无前缀文件均位于忽略目录 `test-results/`。夹具包含合成设备与制品摘要，不生成实际 GPU 启动凭证，也不访问或初始化 GPU。

## 真实 CPU/WASI 兼容性

`judge/backends.py` 的环境标签分支属于共同策略摘要，因此重新运行实际 `sandbox_check`；`test-results/g16-wasi-probes.log` 记录全部 129 个探针通过，凭证写入本轮专用 `g16-wasi-receipt.json`。可信 bundle 为 `px-compose-f5b1318868df/wasi`，摘要 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`。

使用该新凭证串行运行 `RUN_SANDBOX_TESTS=1` 的两项 ML/科学 API→CLI 场景和一项 Agent 正式结果/API→CLI 场景，`g16-real-wasi.log/xml` 记录 3 passed、无跳过。没有使用旧凭证，也没有把组件跳过计为成功。

## 浏览器与排版

现有验收启动器新增 `--gpu-results`：建立一次性 SQLite、合成账号和仅回环 Web 服务，关闭评测；不允许对已有用户数据库运行。中英、390/768/1440 像素及正确 Submit、重复批次失败、公开 Run、损坏结果共二十四组，另查跨账号拒绝、隐藏字段和控制台错误。

首次 `g16-browser-initial.log` 失败来自测试登录按钮选择器命中了页首语言切换按钮；限定到密码表单后，`px-gpu-results-8ef088436249/result.json` 的二十四组和跨账号检查通过。检查手机截图后发现英文预约句子相连，修正模板空白后再次通过，最终记录为 `px-gpu-results-485d9cc49f8b/result.json` 与 `g16-browser-spacing-final.log`。两轮临时数据库均已删除。

`g16-interface.log` 和 `g16-interface/editor/layouts.json` 记录现有编辑器及三十组中英布局检查通过，覆盖练习、知识、目标选项、组织与做题页面的三种屏宽。实际操作验证高亮、诊断、补全、格式化、快捷键、草稿、工具失效回退和格式化期间的新输入保留；核对筛选控件底边、勾选框尺寸/键盘操作与页面无横向溢出。另查看手机英文目标列表、桌面中文练习列表和手机英文 GPU 结果截图。

全仓 Ruff、Python 格式、内容静态检查、无新增迁移及 `git diff --check` 通过。最终源码/凭证/报告身份及本地文档链接检查记录于 `test-results/g16-final-audit.json`。

GPU 执行摘要为 `b8e9070fd4fcab030427738d48633f72c90054989a5c2973f9078bbf7c58f035`；CPU 策略为 `1778ee76eabecef16b1982c97bea384079d15d74d572c93803099db0b87da4b0`。GPU 浏览器报告逐文件记录对应源码摘要；其中合成编译耗时和批次时间不是硬件测量。

## 尚未完成

本轮没有注册或发布 GPU 题库，没有开放网站 GPU 提交，没有执行 CUDA、签发实际 GPU 凭证或测得性能。GPU 容器化部署、准入、官方内容、可比性能榜单，以及独占条件恢复后的完整启动/恢复/队列/部署验收继续待完成。没有改变数据库 schema 或 Compose 配置，也没有修改共享 daemon、驱动或主机服务。
