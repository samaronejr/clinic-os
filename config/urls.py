from apps.core import views
from django.urls import include, path
from django.urls.resolvers import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = [
    path("", views.index, name="index"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("", include("apps.identity.urls")),
]
