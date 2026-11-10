"""Interop routing: the external API and the private management API."""

from django.urls import include, path

from apps.interop.views import manage

app_name = "interop"
urlpatterns = [
    path("api/v1/", include("apps.interop.api.urls")),
    path("api/interop/v1/", manage, name="management"),
]
