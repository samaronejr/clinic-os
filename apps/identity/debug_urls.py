"""Development-only routes for inert identity component review."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.identity import views

urlpatterns: list[URLPattern] = [
    path("__ui__/auth/", views.auth_showcase, name="auth-showcase"),
]
