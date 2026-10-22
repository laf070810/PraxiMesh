from django import forms
from django.utils.translation import gettext_lazy as gettext


class ProfileForm(forms.Form):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    public_name = forms.CharField(label=gettext("公开名称"), max_length=80, required=False)
    bio = forms.CharField(
        label=gettext("公开简介"),
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    is_public = forms.BooleanField(label=gettext("开启公开主页"), required=False)
    confirm_public = forms.BooleanField(
        label=gettext("我确认公开用户名、公开名称和简介；仅主动分享的评测结果进入作品和榜单。"),
        required=False,
    )

    def clean(self):
        values = super().clean()
        if values.get("is_public"):
            if not values.get("public_name"):
                self.add_error("public_name", gettext("开启公开主页前请填写独立的公开名称。"))
            if not values.get("confirm_public"):
                self.add_error("confirm_public", gettext("请明确确认上述公开范围。"))
        return values


class ShareForm(forms.Form):
    profile_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    entry_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    confirm_public = forms.BooleanField(
        label=gettext("我确认公开用户名及这次正式结果，并参与同题目版本、同策略的测试进度榜。")
    )


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
