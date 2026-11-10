"""The URLconf drf-spectacular reads to generate ``docs/api/external-v1.yaml``."""

from django.urls import include, path
from django.urls.resolvers import URLResolver

urlpatterns: list[URLResolver] = [
    path("api/v1/", include("apps.interop.api.urls")),
]
