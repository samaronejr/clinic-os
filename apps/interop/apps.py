"""Django configuration for the interop domain."""

from django.apps import AppConfig


class InteropConfig(AppConfig):
    """Register the interop domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.interop"
