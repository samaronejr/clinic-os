"""Django configuration for the provider capability domain."""

from django.apps import AppConfig


class ProvidersConfig(AppConfig):
    """Register the provider capability domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.providers"
