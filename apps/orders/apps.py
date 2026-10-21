"""Order/result domain registration."""

from django.apps import AppConfig


class OrdersConfig(AppConfig):
    """Keep lab ingestion separate from clinical review and patient release."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"
