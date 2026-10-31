# CUDA 内容契约

`cuda-kernel-v1` 已接入内容校验、不可变版本和现有发布流程。仓库中的两个候选题尚未通过当前完整 GPU 路径的硬件验收，不在默认题库中；结构检查不能证明参考解能编译、通过或具备性能优势。环境、候选包准备和真实验收操作统一见 [GPU 部署指南](deployment/gpu.md#cuda-content)。

两份参考实现及六份针对性错解已在无 GPU 的真实 gVisor guest 中通过 NVRTC 编译，见 [G27](acceptance-g27.md)。该证据只覆盖固定 compute 8.6 目标下的 PTX 生成；错解也可以编译，实际入口、答案和性能仍需完整 GPU 验收。

## 设备函数

共同字段为 `language: "cuda"`、`evaluator: "cuda-kernel-v1"`、`entrypoint: "solve"`。只提交设备源码，平台负责输入、分配、启动及同步。不声明 Python/JavaScript 的 `function`、`interface`、模型或工具字段。

| `operation` | 固定入口 | 输入与启动形状 |
| --- | --- | --- |
| `vector-add-f32-v1` | `extern "C" __global__ void solve(const float* a, const float* b, float* out, int n)` | `inputs` 只含等长 `a`、`b`，长度 0–4096；block 为 `(256,1,1)`，grid 为 `(max(1,ceil(n/256)),1,1)` |
| `matmul-f32-v1` | `extern "C" __global__ void solve(const float* a, const float* b, float* out, int m, int n, int k)` | `inputs` 只含 `a`、`b`、`m`、`n`、`k`；三个维度均为 1–64 的整数。A 为 m×k、B 为 k×n、输出为 m×n，均为行主序。block 为 `(16,16,1)`，grid 为 `(ceil(n/16),ceil(m/16),1)` |

输入数值有限且绝对值不超过 10,000，进入运行时前转换为 float32。每次调用须写完全部有效输出；输出先填充为 NaN，不能依赖上一次调用的内容。长度为零的向量也会启动一个块，内核必须保护边界。`starter` 和 `reference` 均按 UTF-8 字节计数，范围 1–20,000，禁止 NUL。Schema 不分析 C++ 类型；源码需要隔离编译，入口 ABI 和正确性还须通过实际加载与执行验证。

当前固定运行时使用 NVRTC C++17、关闭乘加融合；矩阵参考值逐项执行 float32 乘法和累加。题目提供 `comparison: {"kind": "float", "absolute": ..., "relative": ...}`，两个容差均在 0–0.01。比较采用 `math.isclose`：差值不超过 `max(absolute, relative * max(abs(actual), abs(expected)))`。此范围只适用于 CUDA，不扩大既有 CPU 比较契约。编译选项与语义参考 [NVIDIA NVRTC 13.1.1 文档](https://docs.nvidia.com/cuda/archive/13.1.1/nvrtc/index.html#supported-compile-options)，具体运行边界以仓库固定驱动和隔离检查为准。

## 题目和测试

每题含 2–8 个用例，至少一个公开和一个受保护用例。每个用例只含 `name`、`public`、`inputs`、`expected`；名称唯一且为 1–80 字符，`public` 必须是布尔值。`expected` 是输出的一维数组。校验器重新计算受信数学预期值，拒绝错误答案、形状不符、无穷、NaN 和混合 CPU 字段；该计算不运行提交的源码。

候选扩展 [cuda.json](../content/candidates/cuda.json) 提供向量加法、矩阵乘法、线程索引和行主序概念、一篇教程及一条自由跳转路径。它引用基础目录中的 `vectors`、`matrices` 和 `matrix-multiplication` 等稳定 ID，须与完整基础包合并。默认 `starter.json` 的 50 个节点、26 道题及旧版本不变。针对性错解在 [cuda_incorrect_solutions.json](../tests/cuda_incorrect_solutions.json)：覆盖漏掉块偏移、整数截断、错误运算、错误矩阵跨度与归约维度；源码同样只允许通过隔离 worker 执行。

公开页面和 API 只投影题意、起始代码及公开用例，不含参考解和受保护数据。仓库候选文件本身是公开源码，不能当作对仓库读者保密的考试题。其 `LicenseRef-Pending-Maintainer-Approval` 不代表获得公开再许可授权。

## 发布与验收边界

`content_compose` 只向有效完整包追加新的 ID，拒绝重复 ID、未解析引用、环和未知字段，不修改数据库；输出仍是普通完整 v1 包，且总量不超过 5 MB。输出包含参考解和受保护数据，以私有文件保存，不能放入静态资源目录。修订已有题目应编辑完整包并增加 revision，不能用追加来覆盖旧 ID。

官方发布沿用 `content_import` 的全量替换和不可变版本规则；第三方发布沿用组织命名空间、精确外部依赖及独立审核，仍受原有 75 KB 上传限制。新增 CUDA Schema 不绕过审核，也不启用设备或创建就绪凭证。

真实内容验收顺序为每题的公开参考 Run、正式参考 Submit、每个针对性错解 Submit、恢复参考 Submit。所有任务走正常 API、准入与 GPU worker；核对版本摘要、环境指纹、正式证据、已消耗的整卡预约和完整正确性/性能结果。性能为控制器测得的重复批次往返墙钟时间，包含通信和同步，不是纯内核时间。检查报告不是运行凭证，也不自动发布内容。

实现决定见 [ADR-035](adr/035-cuda-content-and-review.md)。
