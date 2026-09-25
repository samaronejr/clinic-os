"""Register commit-bound invalidation hooks without introducing a tenant table."""

from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    """Optional metadata-only realtime transport."""

    name = "apps.realtime"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Connect hooks once for web, workers and lifecycle commands."""
        from apps.realtime.hooks import connect_hooks  # noqa: PLC0415

        connect_hooks()
