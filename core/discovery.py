"""Public discovery metadata derived from reviewed evaluation contracts."""

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from core.resources import reservation_units

EVALUATORS = {
    "python-function-v1": _("Python 函数"),
    "javascript-function-v1": _("JavaScript 函数"),
    "python-stdio-v1": _("Python 完整程序"),
    "python-ml-linear-v1": _("ML 训练"),
    "python-weighted-fit-v1": _("科学计算与拟合"),
    "python-agent-tools-v1": _("受控工具 Agent"),
}
RUNTIMES = {
    "python-wasi": "Python 3.14.9 / WASI",
    "javascript-wasi": "JavaScript / QuickJS / WASI",
    "python-gvisor": "Python 3.11.15 / gVisor",
}
LIBRARIES = {
    "python-stdlib": _("Python 基础标准库"),
    "javascript-builtins": _("JavaScript 内置对象"),
}


def metadata(version):
    evaluator = version.data["evaluator"]
    python = version.data["language"] == "python"
    supported = settings.JUDGE_BACKEND == "wasi" or (
        settings.JUDGE_BACKEND == "gvisor" and evaluator == "python-function-v1"
    )
    runtime = "python-wasi" if python else "javascript-wasi"
    if supported and settings.JUDGE_BACKEND == "gvisor":
        runtime = "python-gvisor"
    library = "python-stdlib" if python else "javascript-builtins"
    return {
        "evaluator_label": EVALUATORS.get(evaluator, evaluator),
        "runtime": runtime,
        "runtime_label": RUNTIMES[runtime],
        "library": library,
        "library_label": LIBRARIES[library],
        "supported": supported,
        "formal_units": reservation_units(version, "submit"),
    }


def choices(cards, field, labels):
    return [(value, labels.get(value, value)) for value in sorted({p[field] for p in cards})]


def filter_resources(cards, parameters):
    for field in ("runtime", "library"):
        value = parameters.get(field, "")
        if value:
            cards = [card for card in cards if card[field] == value]
    supported = parameters.get("supported", "")
    if supported:
        if supported not in {"1", "0"}:
            return []
        cards = [card for card in cards if card["supported"] == (supported == "1")]
    maximum = parameters.get("max_units", "")
    if maximum:
        if not maximum.isascii() or not maximum.isdecimal() or len(maximum) > 6:
            return []
        cards = [card for card in cards if card["formal_units"] <= int(maximum)]
    return cards
