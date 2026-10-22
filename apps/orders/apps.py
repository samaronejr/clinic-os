"""Order/result domain registration."""

from django.apps import AppConfig


class OrdersConfig(AppConfig):
    """Keep lab ingestion separate from clinical review and patient release."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"

    def ready(self) -> None:
        """Register the Clinical Inbox result feed once models are available."""
        from apps.orders import inbox  # noqa: PLC0415

        inbox.register()
