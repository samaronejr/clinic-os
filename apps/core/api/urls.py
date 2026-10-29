"""Routes of the internal UI API; paths never carry record identifiers."""

from django.urls import path, re_path
from django.urls.resolvers import URLPattern

from apps.core.api import views

app_name = "ui_api"

urlpatterns: list[URLPattern] = [
    path("agenda/query/", views.AgendaQueryView.as_view(), name="agenda-query"),
    path("agenda/board/", views.AgendaBoardQueryView.as_view(), name="agenda-board"),
    path("command/search/", views.CommandSearchView.as_view(), name="command-search"),
    path("inbox/query/", views.InboxQueryView.as_view(), name="inbox-query"),
    path("ehr/autosave/", views.DraftAutosaveView.as_view(), name="ehr-autosave"),
    path(
        "ehr/addendum/open/", views.AddendumOpenView.as_view(), name="ehr-addendum-open"
    ),
    path(
        "ehr/addendum/autosave/",
        views.AddendumAutosaveView.as_view(),
        name="ehr-addendum-autosave",
    ),
    # Last: every other path under the prefix gets the JSON 404 contract body.
    re_path(r"", views.not_found, name="not-found"),
]
