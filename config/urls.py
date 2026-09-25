from apps.core import views
from django.urls import include, path
from django.urls.resolvers import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = [
    path("", views.index, name="index"),
    path("workspace/", views.workspace_home, name="workspace-home"),
    path("sw.js", views.service_worker, name="service-worker"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("api/ui/v1/", include("apps.core.api.urls")),
    path("", include("apps.identity.urls")),
    path("", include("apps.intake.urls")),
    path("", include("apps.scheduling.urls")),
    path("", include("apps.ehr.urls")),
    path("", include("apps.prescription.urls")),
    path("", include("apps.retention.urls")),
    path("", include("apps.billing.urls")),
    path("", include("apps.consent.urls")),
    path("", include("apps.teleconsult.urls")),
]
