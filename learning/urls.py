from django.urls import path

from learning import views

app_name = "learning"

urlpatterns = [
    path("", views.index, name="index"),
    path("achievements/", views.achievements, name="achievements"),
    path("bookmarks/", views.bookmarks, name="bookmarks"),
    path("bookmarks/<slug:slug>/", views.bookmark, name="bookmark"),
    path("notes/", views.notes, name="notes"),
    path("notes/new/", views.note_create, name="note_create"),
    path("notes/<int:pk>/", views.note_detail, name="note_detail"),
    path("notes/<int:pk>/edit/", views.note_edit, name="note_edit"),
    path("notes/<int:pk>/delete/", views.note_delete, name="note_delete"),
    path("reading/", views.reading, name="reading"),
    path("reading/<slug:slug>/", views.reading_mark, name="reading_mark"),
]
