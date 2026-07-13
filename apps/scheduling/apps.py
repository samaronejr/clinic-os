"""Django configuration for the scheduling domain."""

from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    """Register the scheduling domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scheduling"
