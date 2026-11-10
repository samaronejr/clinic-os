"""Insurance domain registration."""

from django.apps import AppConfig


class InsuranceConfig(AppConfig):
    """Register only local, explicitly gated insurance functionality."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.insurance"
