"""Development URL configuration with inert UI review surfaces."""

from django.urls import include, path
from django.urls.resolvers import URLPattern, URLResolver

from config.urls import urlpatterns as production_urlpatterns

urlpatterns: list[URLPattern | URLResolver] = [
    *production_urlpatterns,
    path("", include("apps.identity.debug_urls")),
]
