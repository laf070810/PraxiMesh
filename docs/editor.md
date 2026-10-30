# 浏览器代码编辑器

打开 Python、JavaScript 或维护者提供的 CUDA 练习即可使用编辑器。支持语法高亮、行号、括号匹配与补齐、自动缩进、代码折叠、文件内名称与常用内置名称补全、查找替换、撤销和重做。顶部提供检查、格式化、查找替换按钮；检查提示可定位到对应代码位置。默认题库仍为七类 CPU/WASI 题，CUDA 正式题型尚未发布。

Python 使用 Ruff 检查语法和常见错误，例如未定义名称、未使用的导入；JavaScript 提供语法检查。CUDA 使用扩展的 C++ 语法树，识别设备限定符、共享内存及常用属性，提示括号、声明等结构问题；**不检查类型、头文件、设备能力或运行正确性**，宏与其他扩展也可能需要实际隔离编译确认。CUDA 提示仅为警告，不阻止格式化。

三种语言均支持格式化，CUDA 使用 clang-format，四空格缩进、88 列目标行宽。工具在浏览器内静态分析，不执行代码，也不把代码发送到外部服务。补全来自语言和当前文件，包括 CUDA 线程索引、设备限定符和常用内置函数；并非 AI，也不包含跨文件或运行时类型推断。语言库是否可用仍以题目运行条件为准。

检查提示不会阻止保存、运行或正式提交；只有隔离评测的正式 Submit 才能产生完成记录。格式化可撤销。如果格式化期间继续输入，过期结果会被丢弃。代码长度仍限制为 20000 字符。

| 操作 | 快捷键 |
| --- | --- |
| 保存私有草稿 | Ctrl/Cmd+S |
| 运行公开样例 | Ctrl/Cmd+Enter |
| 格式化 | Shift+Alt+F |
| 打开补全列表 | Ctrl+Space |
| 查找 / 替换 | Ctrl/Cmd+F |
| 切换行注释 | Ctrl/Cmd+/ |
| 撤销 / 重做 | Ctrl/Cmd+Z / Ctrl/Cmd+Shift+Z |
| 移动当前行 | Alt+↑ / Alt+↓ |
| 缩进 / 反向缩进 | Tab / Shift+Tab |
| 离开编辑区 | 先按 Esc，再按 Tab |

保存和运行仍要求登录；worker 离线时运行与提交保持关闭，编辑和保存可继续使用。全部资源随站点分发，不依赖 CDN。首次打开 Python 题目加载 Ruff；首次格式化 CUDA 时才加载 clang-format WASM，CUDA 结构检查无需这个 WASM。工具加载失败会保留代码和草稿操作；编辑器本身加载失败或禁用 JavaScript 时回退为原来的文本框。补全列表展开时也可按 Esc、Tab 离开编辑区。

## 维护前端资源

Compose 和发布镜像直接包含预构建资源，日常启动不需要 Node。修改编辑器源码或依赖的开发者使用 Node 22 或更高版本，在仓库根目录运行：

```sh
npm ci --ignore-scripts
npm run build:editor
npm run test:editor
npm audit
.venv/bin/pytest -q tests/test_editor.py tests/test_localization.py
.venv/bin/python scripts/browser_acceptance.py --interface
.venv/bin/python scripts/browser_acceptance.py --gpu-results
```

源码在 `frontend/`，构建入口为 `scripts/build_editor.mjs`。提交 `package.json`、锁文件及 `static/editor/` 的构建结果、摘要清单和依赖许可证；不提交 `node_modules/`。资源一致性检查防止只修改源码而未重新构建。生成的 JS 包含上游模板字符串中的有效行尾空格，`.gitattributes` 仅对这两个生成文件排除行尾空白检查，不手工改写构建结果。修改中英界面时同步维护 `.po` 和 `.mo`。浏览器依赖安装与完整验证入口见 [README](../README.md#开发与检查)。

CodeMirror 动态样式使用每次响应的 CSP nonce。只有练习详情页面允许同源 worker 和 WASM 编译；不放开内联脚本、任意内联样式或 JavaScript eval。新前端构建文件是发布包的可选源码项，旧制品清单仍可校验。设计见 [RFC-020](rfcs/020-browser-editor.md)。

CUDA 语法扩展在 `scripts/editor_cuda.mjs`，基于锁定的 Lezer C++ 源码生成，不改写学员文本；编辑界面与分析 worker 使用同一语法。上游语法结构变化时构建会要求复核。`npm run test:editor` 测试实际打包 worker 的解析与格式化；浏览器 `--gpu-results` 检查合成 CUDA 页面、键盘、草稿、加载失败及中英布局，使用一次性数据库，不能作为真实 GPU 评测或性能验收。完整的 LLVM/Emscripten/musl 许可补充在 `frontend/clang-format-NOTICES.txt`，构建时合入分发声明。
