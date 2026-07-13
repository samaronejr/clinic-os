"""Django configuration for the comms domain."""

from django.apps import AppConfig


class CommsConfig(AppConfig):
    """Register the comms domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.comms"
