"""Explicit, reviewed backend selection. Failure never selects another backend."""

from django.conf import settings

from judge.sandbox import DockerSandbox, SandboxUnavailable


def configured_sandbox():
    if settings.JUDGE_BACKEND == "gvisor":
        return DockerSandbox(settings.JUDGE_IMAGE)
    if settings.JUDGE_BACKEND == "wasi":
        from judge.wasi import WasiSandbox

        return WasiSandbox(settings.JUDGE_WASI_BUNDLE, settings.JUDGE_WASI_SHA256)
    raise SandboxUnavailable("Unknown judge backend; no fallback is permitted.")


def configured_runtime_description():
    if settings.JUDGE_BACKEND == "wasi":
        return (
            "Python 3.14.9 基础标准库 / JavaScript QuickJS 2026-06-04 同步 JSON 函数；"
            "WASI 不支持网络、文件写入、进程、线程和第三方扩展。"
            "每用例 128 MiB 线性内存、15 秒总等待、32 KiB 输出；运算量超限也会终止。"
        )
    return "Python 3.11.15 标准库；每用例 128 MiB 内存、2 秒 CPU、8 秒总等待、32 KiB 输出。"


def result_environment_label(environment):
    if environment.get("policy") == "wasi-functions-v2":
        return (
            f"CPython {environment['python']} / QuickJS {environment['quickjs']} / WASI · "
            f"Wasmtime {environment['wasmtime']} · sha256:{environment['bundle']}"
        )
    if environment.get("policy") == "wasi-python-function-v1":
        return f"CPython {environment['python']} / WASI · Wasmtime {environment['wasmtime']} · sha256:{environment['bundle']}"
    return environment.get("image", "")
