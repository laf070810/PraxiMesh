# 发布授权状态

PRD §13 将代码许可证、题目/教程文字、社区投稿和外部测试/数据授权留给维护者最终确认。当前未替维护者授予许可证。

- 平台代码：拟议选项 Apache-2.0 或 MIT，待维护者确认。
- 本仓库原创教程/题意：可考虑 CC BY 4.0；参考实现和测试代码需单独约定代码许可。
- 社区投稿：贡献者必须有相应权利，并接受已确定的投稿条款；目前条款尚未生效。
- 第三方依赖：按各自许可证使用，镜像发布需保留相应声明，不能以平台许可证覆盖。

当前 WASI bundle 保留 CPython LICENSE，以及固定 WASI SDK 24 对应的 wasi-libc、musl、cloudlibc、LLVM compiler-rt 声明。原文和不可变来源/摘要见 `vendor/wasi/`。安装到镜像的 Python 包保留各自 distribution 许可元数据。这些记录不替代维护者对最终公开制品授权和再分发义务的复核。

`LicenseRef-Pending-Maintainer-Approval` 是开发状态标记，不是开放许可证。此项不阻止本地研发，但阻止宣称已经完成开源公开发布。
