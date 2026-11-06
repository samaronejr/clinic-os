"""Django configuration for the billing domain."""

from django.apps import AppConfig


class BillingConfig(AppConfig):
    """Register the billing domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"

    def ready(self) -> None:
        """Register only the local recorded adapter; no gate or setting is enabled."""
        from django.conf import settings  # noqa: PLC0415

        from apps.billing.psp_fixtures import RecordedPaymentProvider  # noqa: PLC0415
        from apps.billing.psp_worker import register_payment_provider  # noqa: PLC0415

        secret = getattr(settings, "BILLING_PSP_FIXTURE_SECRET", "")
        register_payment_provider(
            RecordedPaymentProvider(
                secret=secret.encode() if isinstance(secret, str) else b"",
                records={},
            )
        )
