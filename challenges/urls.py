from django.urls import path

from challenges import views

app_name = "challenges"
urlpatterns = [
    path("", views.index, name="index"),
    path("mine/", views.mine, name="mine"),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/revisions/<int:number>/", views.detail, name="revision"),
    path("<slug:slug>/revisions/<int:number>/participate/", views.participate, name="participate"),
    path("<slug:slug>/revisions/<int:number>/tasks/<slug:key>/", views.task, name="task"),
]
