"""Clinic-scoped intake routes that carry no patient state in their paths."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.intake import views

app_name = "intake"

urlpatterns: list[URLPattern] = [
    path(
        "intake/clinics/<uuid:clinic_id>/patients/",
        views.patient_list_view,
        name="patient-list",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/new/",
        views.patient_create_view,
        name="patient-create",
    ),
]
