"""Clinic-scoped retention routes; record selectors stay in POST bodies."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.retention import views

app_name = "retention"

urlpatterns: list[URLPattern] = [
    path(
        "retention/clinics/<uuid:clinic_id>/",
        views.retention_workspace,
        name="status",
    ),
    path("patient/records/", views.patient_records, name="patient-records"),
]
