from django import forms
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.models import KnowledgeNode
from localization.presentation import localized_node


class GoalForm(forms.Form):
    targets = forms.MultipleChoiceField(
        label=_("我的私有概念目标（最多八个）"), widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = []
        for node in KnowledgeNode.objects.filter(published=True, kind="concept"):
            public = localized_node(node)
            label = public["title"]
            if public["translation_fallback"]:
                label += " · " + gettext("中文原文")
            choices.append((node.pk, label))
        self.fields["targets"].choices = choices

    def clean_targets(self):
        values = self.cleaned_data["targets"]
        if len(values) > 8:
            raise forms.ValidationError(_("每份计划最多选择八个目标。"))
        return values


class HintForm(forms.Form):
    question = forms.CharField(
        max_length=1000, widget=forms.Textarea, label=_("想请辅导助手解释的问题")
    )
    hint_level = forms.TypedChoiceField(coerce=int, label=_("我主动查看并同意发送的作者提示层级"))
    include_source = forms.BooleanField(
        required=False,
        label=_("我同意将下面主动粘贴的本人源码片段发送给所示模型服务；默认不发送。"),
    )
    source = forms.CharField(
        max_length=3000,
        required=False,
        widget=forms.Textarea,
        label=_("可选：本人源码片段（最多 3000 字符）"),
    )

    def __init__(self, *args, levels=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["hint_level"].choices = [(n, str(n)) for n in range(levels + 1)]


class ConfirmForm(forms.Form):
    digest = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    confirm = forms.BooleanField(
        label=_("我已检查下方全部输入，同意现在向所示提供方发送一次请求。")
    )
