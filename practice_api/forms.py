from django import forms
from django.utils.translation import gettext_lazy as _


class TokenForm(forms.Form):
    label = forms.CharField(max_length=100, label=_("用途名称"))
    days = forms.IntegerField(min_value=1, max_value=90, initial=30, label=_("有效天数"))
    can_submit = forms.BooleanField(required=False, initial=True, label=_("提交本人的练习"))
    can_read_results = forms.BooleanField(required=False, initial=True, label=_("读取本人的结果"))
    password = forms.CharField(widget=forms.PasswordInput, label=_("当前密码"))

    def clean(self):
        data = super().clean()
        if not data.get("can_submit") and not data.get("can_read_results"):
            raise forms.ValidationError(_("请至少选择一个权限。"))
        return data
