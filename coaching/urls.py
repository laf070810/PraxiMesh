from django.urls import path

from coaching import views

app_name = "coaching"
urlpatterns = [
    path("", views.index, name="index"),
    path("goals/", views.goals, name="goals"),
    path("analysis/", views.analysis, name="analysis"),
    path("diagnosis/<uuid:pk>/", views.diagnosis, name="diagnosis"),
    path("plans/<uuid:pk>/", views.plan, name="plan"),
    path("plans/<uuid:pk>/tasks/<int:version_id>/", views.plan_task, name="plan_task"),
    path("plans/<uuid:pk>/<str:action>/", views.plan_action, name="plan_action"),
    path("hints/new/<int:version_id>/", views.hint_create, name="hint_create"),
    path("hints/<uuid:pk>/", views.hint, name="hint"),
    path("hints/<uuid:pk>/send/", views.hint_send, name="hint_send"),
    path("hints/<uuid:pk>/delete/", views.hint_delete, name="hint_delete"),
]
