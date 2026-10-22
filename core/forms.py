from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm, UsernameField
from django.contrib.auth.password_validation import validate_password


class RegisterForm(UserCreationForm):
    class Meta:
        model = get_user_model()
        fields = ("username",)


class RecoveryForm(forms.Form):
    username = UsernameField(max_length=150, label="用户名")
    recovery_code = forms.CharField(max_length=100, label="恢复码", widget=forms.PasswordInput)
    password = forms.CharField(label="新密码", widget=forms.PasswordInput, max_length=256)

    def clean_password(self):
        password = self.cleaned_data["password"]
        user = get_user_model()(username=self.cleaned_data.get("username", ""))
        validate_password(password, user)
        return password


class SourceForm(forms.Form):
    source = forms.CharField(
        max_length=20000, strip=False, widget=forms.Textarea, label="Python 代码"
    )
    mode = forms.ChoiceField(choices=[("run", "公开样例"), ("submit", "正式提交")])


class ProfileForm(forms.Form):
    display_name = forms.CharField(max_length=80, required=False, label="显示名称（仅自己可见）")
