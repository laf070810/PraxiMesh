from django import forms
from django.db.models import F, Q

from core.models import KnowledgeNode, ProblemVersion
from learning.models import Note


class VersionChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.data['title']} · v{obj.revision}"


class NodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.title


class NoteForm(forms.ModelForm):
    version = VersionChoiceField(
        label="关联题目版本", queryset=ProblemVersion.objects.none(), required=False
    )
    node = NodeChoiceField(
        label="关联知识节点", queryset=KnowledgeNode.objects.none(), required=False
    )

    class Meta:
        model = Note
        fields = ["title", "body", "version", "node"]
        labels = {"title": "标题", "body": "笔记正文"}
        help_texts = {
            "title": "最多 120 个字符。",
            "body": "最多 10,000 个字符，按纯文本保存，仅你可见。",
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
