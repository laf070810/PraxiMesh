from django.urls import path

from community import views

app_name = "community"
urlpatterns = [
    path("", views.index, name="index"),
    path("blogs/", views.blogs, name="blogs"),
    path("blogs/<str:username>/", views.author_blog, name="author_blog"),
    path("mine/", views.mine, name="mine"),
    path("new/", views.post_edit, name="create"),
    path("reports/", views.reports, name="reports"),
    path("appeals/", views.appeals, name="appeals"),
    path("reports/<uuid:pk>/", views.report_detail, name="report_detail"),
    path("appeals/<uuid:pk>/", views.appeal_detail, name="appeal_detail"),
    path("moderation/", views.moderation, name="moderation"),
    path("moderation/<str:kind>/<uuid:pk>/", views.moderate, name="moderate"),
    path("cases/<str:kind>/<uuid:pk>/resolve/", views.resolve_case, name="resolve_case"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/edit/", views.post_edit, name="edit"),
    path("<uuid:post_id>/comments/new/", views.comment_edit, name="comment_create"),
    path("comments/<uuid:pk>/", views.comment_detail, name="comment_detail"),
    path("comments/<uuid:pk>/edit/", views.comment_edit, name="comment_edit"),
    path("writing/<str:kind>/<uuid:pk>/<str:action>/", views.author_action, name="author_action"),
    path("report/<str:kind>/<uuid:pk>/", views.report_create, name="report_create"),
    path("appeal/<str:kind>/<uuid:pk>/", views.appeal_create, name="appeal_create"),
]
