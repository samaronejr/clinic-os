"""Django configuration for the shared shell."""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Register the shell (landing, workspace navigation, health) with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
