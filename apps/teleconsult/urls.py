"""Teleconsult routes: clinic scope only in URLs, never session identifiers."""

from django.urls import path

from apps.teleconsult import views

app_name = "teleconsult"
urlpatterns = [
    path("patient/teleconsult/", views.patient_teleconsult, name="patient"),
    path(
        "teleconsult/clinics/<uuid:clinic_id>/",
        views.staff_teleconsult,
        name="staff",
    ),
]
