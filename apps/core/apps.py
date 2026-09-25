"""Django configuration for the shared shell."""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Register the shell (landing, workspace navigation, health) with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        """Connect the enqueue-time stamp so queue age is measurable (ADR-014)."""
        super().ready()
        from celery.signals import before_task_publish  # noqa: PLC0415

        from .telemetry import stamp_enqueue_timestamp  # noqa: PLC0415

        before_task_publish.connect(
            stamp_enqueue_timestamp,
            dispatch_uid="apps.core.telemetry.stamp_enqueue_timestamp",
        )
