from apps.core import views
from django.urls import path
from django.urls.resolvers import URLPattern

urlpatterns: list[URLPattern] = [
    path("", views.index, name="index"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
]
