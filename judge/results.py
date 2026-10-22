import math

LABELS = {
    "accepted": "通过",
    "wrong_answer": "答案有误",
    "syntax_error": "语法错误",
    "runtime_error": "运行错误",
    "time_limit": "超过时间限制",
    "memory_limit": "超过内存限制",
    "output_limit": "输出过多",
    "internal_error": "评测服务故障，请稍后重试",
}


def matches(actual, expected, comparison, depth=0):
    if depth > 32:
        return False
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        try:
            if not math.isfinite(actual) or not math.isfinite(expected):
                return False
        except OverflowError:
            return False
        if comparison["kind"] == "exact":
            return type(actual) is type(expected) and actual == expected
        return math.isclose(
            actual, expected, rel_tol=comparison["relative"], abs_tol=comparison["absolute"]
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                matches(a, b, comparison, depth + 1) for a, b in zip(actual, expected, strict=True)
            )
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(
                matches(actual[key], value, comparison, depth + 1)
                for key, value in expected.items()
            )
        )
    return False
