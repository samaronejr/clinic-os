"""Django configuration for the prescription domain."""

from django.apps import AppConfig


class PrescriptionConfig(AppConfig):
    """Register the prescription domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.prescription"
