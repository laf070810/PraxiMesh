"""Read-only localized projections. Callers retain responsibility for object access."""

from copy import deepcopy

from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from core.content import digest, public_problem
from localization.models import KnowledgeTranslation, ProblemTranslation

VERDICTS = {
    "accepted": _("通过"),
    "wrong_answer": _("答案有误"),
    "syntax_error": _("语法错误"),
    "runtime_error": _("运行错误"),
    "time_limit": _("超过时间限制"),
    "memory_limit": _("超过内存限制"),
    "output_limit": _("输出过多"),
    "internal_error": _("评测服务故障，请稍后重试"),
}
DIFFICULTIES = {"入门": _("入门"), "基础": _("基础"), "进阶": _("进阶")}


def _locale(locale):
    return (locale or get_language() or "zh-hans").split("-")[0]


def _apply(data, row, locale, source_digest):
    result = deepcopy(data)
    translated = bool(row and row.published)
    if translated:
        result.update({key: value for key, value in row.fields.items() if key != "examples"})
        if "examples" in row.fields:
            for case, name in zip(result["examples"], row.fields["examples"], strict=True):
                case["name"] = name
    result["source_search"] = " ".join(
        [
            data.get("title", ""),
            data.get("summary", ""),
            *data.get("aliases", []),
            *data.get("tags", []),
        ]
    )
    result.update(
        content_language="en" if translated else "zh-hans",
        translation_fallback=locale == "en" and not translated,
        source_digest=source_digest,
    )
    return result


def localized_node(node, locale=None):
    language = _locale(locale)
    checksum = digest(node.data)
    row = (
        KnowledgeTranslation.objects.filter(
            snapshot__node=node, snapshot__source_digest=checksum, locale="en"
        )
        .order_by("-revision")
        .first()
        if language == "en"
        else None
    )
    return _apply(node.data, row, language, checksum)


def localized_problem(version, locale=None):
    language = _locale(locale)
    row = (
        ProblemTranslation.objects.filter(version=version, locale="en")
        .order_by("-revision")
        .first()
        if language == "en"
        else None
    )
    result = _apply(public_problem(version), row, language, version.digest)
    result["difficulty_label"] = DIFFICULTIES.get(result["difficulty"], result["difficulty"])
    return result


def runtime_description(version=None):
    from django.conf import settings
    from django.utils.translation import gettext

    if (
        version
        and settings.JUDGE_BACKEND != "wasi"
        and version.data["evaluator"] != "python-function-v1"
    ):
        return gettext("当前评测环境不支持这道题的运行方式；仍可阅读、编辑和保存草稿。")
    if settings.JUDGE_BACKEND == "wasi":
        if version and version.data["evaluator"] == "python-agent-tools-v1":
            return gettext(
                "Python / WASI 受控工具任务；每轮独立实例，最多6轮。每轮墙钟5秒、每场景墙钟20秒、累计驱动CPU 6秒；输出每轮4096字节。没有网络、外部账户或系统命令工具。"
            )
        if version and version.data["language"] == "javascript":
            return gettext(
                "JavaScript / QuickJS 2026-06-04；只支持同步 JSON 函数，不提供 Node.js、DOM、网络或模块导入。每用例 128 MiB 线性内存、15 秒总等待、32 KiB 输出；运算量超限也会终止。"
            )
        if version and version.data["evaluator"] == "python-stdio-v1":
            return gettext(
                "Python 3.14.9 / WASI 完整程序；从标准输入读取文本，标准输出须恰为一个 JSON 值。编译检查与运行分别限 15 秒、128 MiB 线性内存；输出上限 32 KiB。没有网络、文件写入、进程、线程或第三方扩展。"
            )
        return gettext(
            "Python 3.14.9 / WASI 基础标准库；不支持网络、文件写入、进程、线程和第三方扩展。每用例 128 MiB 线性内存、15 秒总等待、32 KiB 输出；运算量超限也会终止。"
        )
    return gettext(
        "Python 3.11.15 标准库；每用例 128 MiB 内存、2 秒 CPU、8 秒总等待、32 KiB 输出。"
    )


def localized_public_cases(version, result):
    data = localized_problem(version)
    names = {
        before["name"]: after["name"]
        for before, after in zip(public_problem(version)["examples"], data["examples"], strict=True)
    }
    return [
        {
            **case,
            "name": names.get(case["name"], case["name"]),
            "label": VERDICTS.get(case.get("verdict"), ""),
        }
        for case in result.get("public_cases", [])
    ]


def result_label(job):
    from django.utils.translation import gettext

    if (
        job.verdict == "internal_error"
        and job.result.get("label") == "等待评测超时，请稍后重新提交。"
    ):
        return gettext("等待评测超时，请稍后重新提交。")
    return VERDICTS.get(job.verdict, "")
