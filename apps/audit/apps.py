"""Django configuration for the audit domain."""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Register the audit domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
