"""Clinic-scoped clinical workspace; record selection travels only in POST bodies."""

from django.urls import path

from apps.ehr.attachment_views import attachment_workspace
from apps.ehr.history_views import history_workspace
from apps.ehr.views import encounter_workspace

app_name = "ehr"
urlpatterns = [
    path(
        "ehr/clinics/<uuid:clinic_id>/attachments/",
        attachment_workspace,
        name="attachments",
    ),
    path("ehr/clinics/<uuid:clinic_id>/history/", history_workspace, name="history"),
    path(
        "ehr/clinics/<uuid:clinic_id>/encounter/", encounter_workspace, name="encounter"
    ),
]
