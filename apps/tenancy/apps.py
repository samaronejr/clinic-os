"""Django configuration for the tenancy domain."""

from django.apps import AppConfig


class TenancyConfig(AppConfig):
    """Register the tenancy domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenancy"
