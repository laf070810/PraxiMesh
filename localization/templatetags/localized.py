from django import template

from localization.presentation import DIFFICULTIES, VERDICTS, localized_node, localized_problem

register = template.Library()


@register.filter
def problem_text(version):
    return localized_problem(version) if version else {}


@register.filter
def node_text(node):
    return localized_node(node) if node else {}


@register.filter
def verdict_text(value):
    return VERDICTS.get(value, "")


@register.filter
def difficulty_text(value):
    return DIFFICULTIES.get(value, value)


def _field(data, field):
    from django.utils.html import format_html
    from django.utils.translation import gettext

    # This tag never accepts arbitrary/private field names from requests.
    if field not in {"title", "summary", "statement", "constraints"}:
        return ""
    text = data.get(field, "")
    if data.get("translation_fallback"):
        return format_html(
            '<span lang="zh-hans">{}</span> <small class="translation-fallback">{}</small>',
            text,
            gettext("中文原文"),
        )
    return text


@register.simple_tag
def problem_field(version, field):
    return _field(localized_problem(version), field) if version else ""


@register.simple_tag
def node_field(node, field):
    return _field(localized_node(node), field) if node else ""


@register.filter
def problem_title(version):
    return _field(localized_problem(version), "title") if version else ""


@register.filter
def node_title(node):
    return _field(localized_node(node), "title") if node else ""


@register.filter
def result_text(job):
    from localization.presentation import result_label

    return result_label(job)
