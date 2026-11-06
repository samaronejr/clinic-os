"""Clinic navigation only; patient, order and result selectors are POST-only."""

from django.urls import path

from apps.orders import callback_views, patient_views, source_views, views

app_name = "orders"
urlpatterns = [
    path("clinics/<uuid:clinic_id>/results/", views.results, name="results"),
    path(
        "clinics/<uuid:clinic_id>/results/source/", source_views.download, name="source"
    ),
    path("orders/lab/callback/", callback_views.lab_callback, name="lab-callback"),
    path(
        "patient/results/", patient_views.patient_results_view, name="patient-results"
    ),
]
