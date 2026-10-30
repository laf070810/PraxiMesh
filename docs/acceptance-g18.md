# G18：CUDA 编辑、公开题目包与资源筛选

日期：2026-10-31。范围为 PRD §3.3、§5、§8.4/8.5、R-07/15/16，设计见 [RFC-020 补充](rfcs/020-browser-editor.md#cuda-编辑与资源显示补充2026-10-31)与 [ADR-034](adr/034-cuda-editor-and-public-tools.md)。当前正在完成本批验收；本页只记录已经执行的结果。

## 交付范围

- CUDA 沿用站内 CodeMirror 编辑器、私有草稿、查找替换、快捷键及失败回退。锁定的 C++ 语法扩展支持 CUDA 限定符与常用属性；文件内名称、CUDA 关键字和内置函数可补全。结构提示为警告，不是 NVRTC 类型、头文件、设备能力或正确性检查。
- clang-format WASM 按需在同源 worker 内格式化，固定文件名、四空格和 88 列目标行宽；保留撤销、修订冲突和长度上限。不执行源码，不访问外部服务，随制品保留 LLVM/Emscripten/musl 等许可。
- API v1 公开包可下载私有 `solution.cu`，校验固定操作、入口、容差、输入形状及公开预期结果，拒绝私有字段与混合契约。远程提交沿用既有令牌、版本和幂等规则；本地 `test` 明确拒绝 CUDA，不改用 WASI 或宿主编译器。
- 题库显式显示 CUDA 运行条件，CPU 调用单位与 GPU 整卡秒数独立筛选。GPU 兼容状态来自现有只读准入结果，未更改评分或开放策略。

## 原始失败与修复

普通 C++ 解析器会误报合法 CUDA 限定符；改为构建时扩展锁定语法，并让界面与检查 worker 使用同一生成结果。首次构建的 `__restrict` 与上游 Microsoft 指针修饰符冲突，删除重复定义后构建通过。首轮独立 worker 测试误以为大写宏必然产生诊断；上游实际允许大写宏，修正测试输入后四项实际解析/格式化检查通过。公开包首轮为 **100 passed、1 failed、1 skipped**：失败断言把整个筛选栏中的其他运行选项当成 GPU 卡片文案，改为只核对题目列表。

浏览器实际发现补全弹窗消费 Esc 后，Tab 仍会缩进。普通快捷键处理优先级不足，改为 DOM 事件观察器保持 Esc 后的临时焦点模式；各次失败日志和截图保留在 `test-results/`，后续结果见下方。

真实异步格式化还暴露了后台检查过早覆盖“格式化期间已修改”提示的问题。过期格式化结果原本已正确丢弃，现将提示保留到下次编辑或主动操作；继续更新当前修订的诊断，不覆盖新源码。重跑后通过。Compose 验收的健康断言仍使用 G17 前的两个字段，已复现并补上 `gpu_evaluation: unavailable`；新增对镜像实际 HTTP 分发的全部编辑器资产摘要及 WASM MIME 类型核对。

## 验证结果

- 公开包、筛选、API、GPU 准入与翻译的首轮定向回归：**171 passed、1 skipped**，见 `test-results/g18-public-final.xml`。跳过项为真实 WASI CLI；已通过可信 bundle 补跑正确/错误解，**1 passed**，见 `test-results/g18-real-cli.xml`。随后包含新增公开包身份/健康、编辑器资源和发布兼容性的联合检查 **213 passed、无跳过**，见 `test-results/g18-component-final.xml`。
- 实际打包的 worker：四项 CUDA 语法、UTF-16 位置、真实 clang-format、幂等与扩展语法提示检查通过，见 `test-results/g18-editor-worker-final.log`。
- CUDA 浏览器最终通过：`test-results/px-gpu-results-ff35d1ab207f/result.json`。覆盖 24 个中英结果页面、6 个关闭准入的做题页面、6 个题库筛选布局，以及中英各 12 项编辑交互；宽度 390/768/1440，未发现横向溢出或同排筛选控件底边错位。已目视检查手机筛选与桌面编辑截图，另验证跨账号拒绝；一次性数据库已删除。
- Compose `--ai --browsers` 验收仍在本批执行；完成后更新本节。源码浏览器成功不替代镜像、真实 CPU 模型或重建保留验收。

## 身份与边界

执行策略未变：CPU `0b8f747585cea5e0601502ca3fe7a271ee15650901f876d420eae64d58999192`，GPU `26de9b4fa013aa197d54fb43bb61d67224603f0a2743250cb950e0f40594eaa3`。前端源码与输出身份在 `static/editor/manifest.json`，依赖版本与完整性在 npm 锁文件。

没有占用 GPU，没有生成 GPU 就绪凭证。浏览器使用一次性数据库中的合成 CUDA 题目和结果；默认官方内容仍为 50 个节点、26 道 CPU/WASI 题。完整 CUDA 启动、执行与异常恢复、性能、正式内容、GPU Compose/发布协调器、本地 GPU 执行及公开榜单仍未验收或交付。旧 GPU 记录不替代当前完整路径，完整产品目标继续以[需求追踪](requirements.md)为准。
