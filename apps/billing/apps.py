"""Django configuration for the billing domain."""

from django.apps import AppConfig


class BillingConfig(AppConfig):
    """Register the billing domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
