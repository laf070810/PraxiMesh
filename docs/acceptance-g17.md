# G17：GPU 健康准入与部署排空

2026-10-31。依据 [RFC-019](rfcs/019-gpu-kernel-evaluation.md#网页准入与部署排空)和 [ADR-033](adr/033-gpu-admission-and-drain.md)，操作集中在 [GPU 部署指南](deployment/gpu.md#gpu-admission)。继续遵循不占 GPU 的安排；本轮设备身份和 GPU 成绩仅来自一次性数据库中的合成夹具。

## 实现范围

`JUDGE_GPU_DEVICES` 默认空，显式允许最多十六个完整 UUID。GPU 准入要求总开关、完整启动发布的环境、设备和执行指纹、当前策略及新鲜心跳一致；忙碌设备还需有效原任务租约及固定预约单位。Web 只读设备表，不访问私有凭证或 GPU。缺少或无法读取完整 GPU 策略源码时只关闭 GPU，CPU 健康检查继续可用。

队列在既有锁下检查版本、待处理上限及独立 GPU 日额度，并按健康设备的未完成任务数选择固定指纹。幂等重试不增加预约，后续失联不自动改投 CPU 或另一设备。结果 API 同时提供明确区分的 CPU/GPU 余额；做题页按 CUDA 输入数组、输出数组、设备内核入口及整卡秒数展示。

迁移 `core.0005_gpu_admission_environment` 增加环境列，旧记录为空，原所有权及预约保留。全部启动检查成功后，环境与 ready 在同一事务发布；换所有者清空原发布信息。`judge_admission` 的暂停覆盖两类新提交，状态区分执行池，排空额外等待全部物理预约；任务结束、账号删除或移除配置都不能隐藏预约。恢复要求健康、兼容的池及无失效物理预约。

## 原始失败与组件结果

- `test-results/g17-admission-initial.log/xml`：两项失败分别复现结果 API 缺失 GPU 额度，以及任务删除后部署排空遗漏设备预约。修复后通过。
- `g17-admission-component-initial.log/xml`：65 passed，覆盖初始修复、暂停、启动与 worker。`g17-gates-initial.log/xml`：29 passed，覆盖失联、旧策略、环境损坏、未来心跳、语言不符、负载选择、独立额度、API 幂等、跨账号、CSRF 和健康字段。
- `g17-policy-package-initial.log/xml`：两项失败复现 GPU 策略文件不存在或不可读时错误逸出。修复为拒绝 GPU 容量，CPU 健康接口保持正常；定向重放见 PostgreSQL 补充结果。
- `g17-component-final.log/xml`：330 passed、7 skipped，包含 GPU 证据/凭证、独立资源、API、翻译、Web、发布协调器、发布包、自部署初始化、SQLite 迁移和连接超时恢复。七个跳过均为 PostgreSQL 专属场景，已在下述真实 PostgreSQL 两轮中执行，未计为组件通过。
- `g17-float-display-initial.log/xml`：一项失败复现 GPU 的 `float` 比较契约落入页面严格相等分支。修正展示后，`g17-display-final.log/xml` 的 101 项准入、翻译和 Web 检查通过；未改变 GPU 执行或数值比较实现。

## 实际 PostgreSQL

首次 `g17-postgres-initial.log/xml` 为 135 passed、1 failed、1 skipped。原子发布检查确认事务内的另一连接仍看见隔离停用，提交后却未获得准入；原因是旧启动协议夹具只保存简化的制品描述。将该夹具改为与持久证据测试共用的完整合成身份后通过，未放宽生产校验。

`g17-postgres-final.log/xml` 为 200 passed、1 skipped，覆盖并发 GPU 预约、Web 仅 SELECT 即可准入、worker 原权限、发布原子性、旧预约迁移、worker 领取/结算锁、暂停/超时及数据库角色配置。唯一跳过是 SQLite 专属 busy-timeout 恢复，它已在上述组件检查执行。

策略包缺失修正后的 `g17-postgres-followup.log/xml` 为 48 passed、无跳过，重新验证 GPU 门槛、实际并发/权限/迁移，并补做 CPU 并发额度和 Web/worker 生产权限。两项竞争 GPU Submit 在 180 秒额度下只接收一项，另一项返回 429。三次自有 PostgreSQL 容器和卷均已清理。

## 真实 CPU/WASI

队列、模型与部署命令改变共享策略，因此重新执行实际 `sandbox_check`。`g17-wasi-probes.log` 的 129 个探针全部通过，使用本轮新 `g17-wasi-receipt.json`。可信 bundle 仍为 `px-compose-f5b1318868df/wasi`，摘要 `ad9edfbe0cc8a4996789175f8fcb42feee618c8b4b21a8618858f5bf1fdd595d`。

随后串行重放两项 ML/科学 API 公开包→本地工具，以及一项 Agent 入队→真实 worker→正式证据→API/本地工具，`g17-real-wasi.log/xml` 为 3 passed、无跳过。GPU 状态没有使用 WASI 记录充当启动证明。

## 浏览器与交付检查

浏览器使用现有隔离启动器，只在新建测试数据库写入合成 GPU 成绩，评测关闭，设备允许列表明确为空。中英、390/768/1440 像素的二十四组结果和六组做题页检查通过，验证预算单位、公开数组/容差、关闭按钮、无横向溢出、无隐藏字段及跨账号拒绝。

早期报告 `px-gpu-results-9730f285bb2e`、`px-gpu-results-9d889b804ace` 及 `px-gpu-results-11b817b20288` 保留原始截图与修正过程。实际手机截图发现原文标记紧贴标题、运行说明缺少句间隔及浮点容差显示错误；修正后核对最终中英手机页面。原文标记样式限定为 `small.translation-fallback`，不会改变整段翻译回退提示。

最终浏览器报告位置及当前源码摘要见 `test-results/g17-final-audit.json`。`g17-browser-delivery.log` 对应 GPU 界面；`g17-interface-delivery.log` 与 `g17-interface-delivery/editor/layouts.json` 对应既有编辑器和三十组标题、筛选及选项列表布局。最终审查同时核对报告逐文件摘要、临时数据库清理、当前 WASI 凭证、本地文档链接和跨矩阵跳过项的实际重放。

全仓 Ruff、Python 格式、CSS Prettier、内容静态检查、迁移一致性及差异空白检查通过。GPU 执行摘要为 `26de9b4fa013aa197d54fb43bb61d67224603f0a2743250cb950e0f40594eaa3`，CPU 策略为 `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`。

## 范围与后续

没有修改真实部署的设备名单、用户数据库、共享 daemon、驱动或主机服务；没有 GPU 工作负载、实际 GPU 凭证或性能测量。官方目录仍为 26 道 CPU/WASI 题，GPU schema/内容发布、CUDA 编辑工具、性能榜单与容器部署仍待完成，完整 PRD 目标保持实施中。

数据库增列在实际 SQLite/PostgreSQL 验证；现有表级角色权限已覆盖新增列。`Dockerfile` 的 `COPY core` 与发布器对 `core/` 的递归收录包含新模块和迁移，本批发布/初始化单元检查通过；未把它当作新冻结镜像、Compose GPU 部署或完整生产升级验收。

排空统计的是数据库中的任务及其持久设备预约，不证明独立宿主 worker 的启动维护服务已经退出。完整 GPU 部署仍须连接这些进程的监管、停止、清理和旧环境冻结；本批不把现有 CPU 发布协调器称为 GPU 部署方案。
