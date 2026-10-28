"""Clinic-scoped organization controls; artifact selectors never travel in URLs."""

from django.urls import path

from apps.ai.views import settings_view

app_name = "ai"
urlpatterns = [
    path("clinics/<uuid:clinic_id>/settings/ai/", settings_view, name="settings")
]
