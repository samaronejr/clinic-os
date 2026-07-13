"""Django configuration for the consent domain."""

from django.apps import AppConfig


class ConsentConfig(AppConfig):
    """Register the consent domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.consent"
