from django import forms
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from core.models import KnowledgeNode, ProblemVersion
from learning.models import Note
from localization.presentation import localized_node, localized_problem


class VersionChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        data = localized_problem(obj)
        suffix = str(_("中文原文")) if data["translation_fallback"] else ""
        return f"{data['title']} · v{obj.revision} {suffix}"


class NodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        data = localized_node(obj)
        suffix = str(_("中文原文")) if data["translation_fallback"] else ""
        return f"{data['title']} {suffix}"


class NoteForm(forms.ModelForm):
    version = VersionChoiceField(
        label=_("关联题目版本"), queryset=ProblemVersion.objects.none(), required=False
    )
    node = NodeChoiceField(
        label=_("关联知识节点"), queryset=KnowledgeNode.objects.none(), required=False
    )

    class Meta:
        model = Note
        fields = ["title", "body", "version", "node"]
        labels = {"title": _("标题"), "body": _("笔记正文")}
        help_texts = {
            "title": _("最多 120 个字符。"),
            "body": _("最多 10,000 个字符，按纯文本保存，仅你可见。"),
        }
        widgets = {"body": forms.Textarea(attrs={"rows": 12, "maxlength": 10000})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        versions = Q(problem__published=True, pk=F("problem__current_id"))
        nodes = Q(published=True)
        # An existing private note can retain its original withdrawn/old target.
        # Other unpublished or old targets cannot be selected through a forged POST.
        if self.instance.pk:
            if self.instance.version_id:
                versions |= Q(pk=self.instance.version_id)
            if self.instance.node_id:
                nodes |= Q(pk=self.instance.node_id)
        self.fields["version"].queryset = ProblemVersion.objects.filter(versions).order_by(
            "problem_id", "revision"
        )
        self.fields["node"].queryset = KnowledgeNode.objects.filter(nodes).order_by("id")
