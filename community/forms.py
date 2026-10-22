from django import forms

from community.models import Appeal, Post, Report
from core.models import ProblemVersion


class VersionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.problem_id} · v{obj.revision}"


class PostForm(forms.Form):
    kind = forms.ChoiceField(choices=Post.Kind.choices, label="内容类型")
    title = forms.CharField(max_length=160, label="标题")
    version = VersionChoice(
        queryset=ProblemVersion.objects.none(), required=False, label="关联题目版本（题解必选）"
    )
    body = forms.CharField(max_length=20000, widget=forms.Textarea, label="正文（纯文本）")
    revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["version"].queryset = ProblemVersion.objects.filter(
            problem__published=True, problem__current__isnull=False
        ).order_by("problem_id", "-revision")

    def clean(self):
        data = super().clean()
        if data.get("kind") == Post.Kind.SOLUTION and not data.get("version"):
            self.add_error("version", "题解必须选择明确的题目版本。")
        return data


class CommentForm(forms.Form):
    body = forms.CharField(max_length=5000, widget=forms.Textarea, label="评论（纯文本）")
    revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class ModerationForm(RevisionForm):
    action = forms.ChoiceField(
        choices=[("publish", "审核通过"), ("reject", "不通过"), ("hide", "隐藏已公开内容")]
    )
    note = forms.CharField(max_length=1000, widget=forms.Textarea, label="给作者的处理说明")


class ReasonForm(forms.Form):
    reason = forms.CharField(
        max_length=2000, widget=forms.Textarea, label="说明（仅本人及审核者可见）"
    )
    revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)


class ReportDecisionForm(forms.Form):
    decision = forms.ChoiceField(
        choices=[(Report.Status.RESOLVED, "已处理"), (Report.Status.DISMISSED, "未采纳")]
    )
    note = forms.CharField(max_length=1000, widget=forms.Textarea, label="给举报人的处理说明")


class AppealDecisionForm(forms.Form):
    decision = forms.ChoiceField(
        choices=[
            (Appeal.Status.REOPENED, "接受申诉，重新送审"),
            (Appeal.Status.UPHELD, "维持原决定"),
        ]
    )
    note = forms.CharField(max_length=1000, widget=forms.Textarea, label="给申诉人的处理说明")
