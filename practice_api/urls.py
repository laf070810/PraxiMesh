from django.urls import path

from practice_api import views

app_name = "practice_api"
urlpatterns = [
    path("api/v1/problems/", views.problem_list, name="problems"),
    path("api/v1/problems/<slug:slug>/", views.problem_package, name="problem"),
    path("api/v1/problems/<slug:slug>/submissions/", views.submit, name="submit"),
    path("api/v1/submissions/<uuid:job_id>/", views.result, name="result"),
    path("accounts/tokens/", views.tokens, name="tokens"),
    path("accounts/tokens/<uuid:token_id>/revoke/", views.revoke, name="revoke"),
]
