"""Django configuration for the identity domain."""

from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Register the identity domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"
