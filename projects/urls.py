from django.urls import path

from projects import views

app_name = "projects"
urlpatterns = [
    path("", views.index, name="index"),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/revisions/<int:number>/", views.detail, name="revision"),
    path("<slug:slug>/revisions/<int:number>/stages/<slug:key>/", views.stage, name="stage"),
]
