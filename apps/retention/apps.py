"""Django configuration for the retention domain."""

from django.apps import AppConfig


class RetentionConfig(AppConfig):
    """Register the retention domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.retention"
