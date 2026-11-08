"""CRM application wiring."""

from django.apps import AppConfig


class CrmConfig(AppConfig):
    """Register purpose-specific marketing checks at both send boundaries."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crm"

    def ready(self) -> None:
        """Keep all marketing sends consent gated, including inbox sends."""
        from apps.comms.inbox_services import register_marketing_opt_in  # noqa: PLC0415
        from apps.crm.campaigns import marketing_opted_in  # noqa: PLC0415

        register_marketing_opt_in(marketing_opted_in)
