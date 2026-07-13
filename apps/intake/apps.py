"""Django configuration for the intake domain."""

from django.apps import AppConfig


class IntakeConfig(AppConfig):
    """Register the intake domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.intake"
