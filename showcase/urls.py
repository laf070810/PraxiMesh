from django.urls import path

from showcase import views

app_name = "showcase"
urlpatterns = [
    path("settings/", views.settings, name="settings"),
    path("settings/close/", views.close, name="close"),
    path("mine/", views.mine, name="mine"),
    path("share/<uuid:submission_id>/", views.share, name="share"),
    path("withdraw/<uuid:pk>/", views.withdraw, name="withdraw"),
    path("profiles/<str:username>/", views.profile, name="profile"),
    path("results/<uuid:pk>/", views.entry, name="entry"),
    path("boards/", views.boards, name="boards"),
    path("boards/<int:version_id>/<str:fingerprint>/", views.board, name="board"),
]
