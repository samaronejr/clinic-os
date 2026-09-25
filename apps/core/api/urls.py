"""Routes of the internal UI API; paths never carry record identifiers."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.core.api import views

app_name = "ui_api"

urlpatterns: list[URLPattern] = [
    path("agenda/query/", views.AgendaQueryView.as_view(), name="agenda-query"),
]
