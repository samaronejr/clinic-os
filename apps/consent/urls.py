"""Consent routes: clinic scope only in URLs, never patient identifiers."""

from django.urls import path

from apps.consent import views

app_name = "consent"
urlpatterns = [
    path("patient/consent/", views.patient_consent, name="patient"),
    path("clinics/<uuid:clinic_id>/consent/", views.staff_consent, name="staff"),
]
