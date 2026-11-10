"""Clinic-only routes; record and query selectors remain in POST bodies."""

from django.urls import path

from apps.analytics.views import reports

app_name = "analytics"
urlpatterns = [path("reports/clinics/<uuid:clinic_id>/", reports, name="reports")]
