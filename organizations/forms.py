import json

from django import forms
from django.utils.translation import gettext_lazy as _

from organizations.models import Role
from organizations.packages import MAX_PACKAGE_BYTES, parse_payload
from organizations.services import OrganizationError


class OrganizationForm(forms.Form):
    namespace = forms.SlugField(max_length=24, label=_("组织命名空间"))
    name = forms.CharField(max_length=120, label=_("组织名称"))
    description = forms.CharField(
        max_length=2000, required=False, widget=forms.Textarea, label=_("组织说明")
    )


class InvitationForm(forms.Form):
    username = forms.CharField(max_length=150, label=_("受邀账号用户名"))
    role = forms.ChoiceField(choices=Role.choices, label=_("组织角色"))


class RoleForm(forms.Form):
    role = forms.ChoiceField(choices=Role.choices, label=_("组织角色"))


class CourseForm(forms.Form):
    slug = forms.SlugField(max_length=48, label=_("课程标识"))
    plan = forms.CharField(
        max_length=20000, widget=forms.Textarea, label=_("固定版本课程计划（JSON）")
    )
    acknowledge = forms.BooleanField(
        label=_("我理解课程计划由组织维护，删除个人账号不会删除组织的版本记录。")
    )

    def clean_plan(self):
        try:
            return parse_payload(self.cleaned_data["plan"].encode())
        except (OrganizationError, UnicodeError):
            raise forms.ValidationError(_("请输入有效、有限且没有重复键的 JSON。")) from None


class PackageForm(forms.Form):
    slug = forms.SlugField(max_length=24, label=_("题库包标识"))
    payload = forms.FileField(label=_("题库包 JSON（最多 75 KB）"))
    acknowledge = forms.BooleanField(
        label=_(
            "我确认来源与许可声明准确；组织保留提交的版本，技术审核不代表获准公开运营或再许可。"
        )
    )

    def clean_payload(self):
        uploaded = self.cleaned_data["payload"]
        try:
            return parse_payload(uploaded.read(MAX_PACKAGE_BYTES + 1))
        except OrganizationError:
            raise forms.ValidationError(
                _("题库包必须是最多 75 KB 的有效 UTF-8 JSON，且不能包含重复键。")
            ) from None


class DigestForm(forms.Form):
    digest = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)


class ReviewForm(DigestForm):
    action = forms.ChoiceField(
        choices=[("approve", _("技术审核通过")), ("reject", _("退回修订"))], label=_("审核决定")
    )
    note = forms.CharField(
        max_length=1000, widget=forms.Textarea, label=_("给组织维护者的审核说明")
    )
    acknowledge = forms.BooleanField(
        label=_("我已独立核对当前摘要、内容与来源声明；此决定不替代公开运营及版权许可。")
    )


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)
