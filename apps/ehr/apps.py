"""Django configuration for the ehr domain."""

from django.apps import AppConfig


class EhrConfig(AppConfig):
    """Register the ehr domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ehr"
