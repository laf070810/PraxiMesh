from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.i18n import JavaScriptCatalog

from core import views

urlpatterns = [
    path("challenges/", include("challenges.urls")),
    path("coaching/", include("coaching.urls")),
    path("organizations/", include("organizations.urls")),
    path("projects/", include("projects.urls")),
    path("showcase/", include("showcase.urls")),
    path("", include("practice_api.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
    path("learning/", include("learning.urls")),
    path("community/", include("community.urls")),
    path("", views.home, name="home"),
    path("learn/", views.learn, name="learn"),
    path("learn/<slug:slug>/", views.node_detail, name="node"),
    path("learn/<slug:slug>/enroll/", views.enroll, name="enroll"),
    path("practice/", views.practice, name="practice"),
    path("problems/<slug:slug>/", views.problem_detail, name="problem"),
    path("problems/<slug:slug>/draft/", views.save_draft, name="draft"),
    path("problems/<slug:slug>/submit/", views.submit, name="submit"),
    path("drafts/<int:draft_id>/", views.draft_detail, name="draft_detail"),
    path("submissions/<uuid:job_id>/", views.submission_detail, name="submission"),
    path("api/submissions/<uuid:job_id>/", views.submission_status, name="submission_status"),
    path("me/", views.dashboard, name="dashboard"),
    path("accounts/register/", views.register, name="register"),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/recover/", views.recover, name="recover"),
    path("accounts/profile/", views.account, name="account"),
    path("accounts/recovery-code/", views.rotate_recovery, name="rotate_recovery"),
    path("accounts/export/", views.export_account, name="export_account"),
    path("accounts/delete/", views.delete_account, name="delete_account"),
    path("health/", views.health, name="health"),
]
