"""Install the governed reporting domain."""

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    """Reports never register an external provider or an autonomous action."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
