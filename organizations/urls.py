from django.urls import path

from organizations import views

app_name = "organizations"
urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path(
        "<slug:org_id>/package-revisions/<int:pk>/cancel/",
        views.package_cancel,
        name="package_cancel",
    ),
    path("packages/", views.published_packages, name="published_packages"),
    path("packages/<uuid:pk>/", views.public_package, name="public_package"),
    path("review/", views.review_queue, name="review_queue"),
    path("review/<int:pk>/", views.review, name="review"),
    path("invitations/<uuid:pk>/<str:action>/", views.invitation_action, name="invitation_action"),
    path("<slug:org_id>/", views.detail, name="detail"),
    path("<slug:org_id>/invite/", views.invite, name="invite"),
    path("<slug:org_id>/members/<int:pk>/<str:action>/", views.member_action, name="member_action"),
    path("<slug:org_id>/courses/new/", views.course_create, name="course_create"),
    path("<slug:org_id>/courses/<uuid:pk>/", views.course, name="course"),
    path(
        "<slug:org_id>/courses/<uuid:pk>/revisions/<int:number>/",
        views.course,
        name="course_revision",
    ),
    path(
        "<slug:org_id>/courses/<uuid:pk>/revisions/<int:number>/units/<slug:key>/",
        views.course_unit,
        name="course_unit",
    ),
    path(
        "<slug:org_id>/course-revisions/<int:pk>/<str:action>/",
        views.course_action,
        name="course_action",
    ),
    path("<slug:org_id>/enrollments/<int:pk>/<str:action>/", views.enrollment, name="enrollment"),
    path("<slug:org_id>/packages/new/", views.package_create, name="package_create"),
    path(
        "<slug:org_id>/packages/<uuid:pk>/revisions/<int:number>/",
        views.package_revision,
        name="package_revision",
    ),
    path(
        "<slug:org_id>/package-revisions/<int:pk>/submit/",
        views.package_submit,
        name="package_submit",
    ),
    path(
        "<slug:org_id>/packages/<uuid:pk>/withdraw/",
        views.package_withdraw,
        name="package_withdraw",
    ),
]
