"""Django configuration for the teleconsult domain."""

from django.apps import AppConfig


class TeleconsultConfig(AppConfig):
    """Register the teleconsult domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.teleconsult"
