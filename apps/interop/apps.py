"""Django configuration for the interop domain."""

from config.celery import app
from django.apps import AppConfig


class InteropConfig(AppConfig):
    """Register the interop domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.interop"

    def ready(self) -> None:
        """Register the webhook action, its send-time recheck and export sweeps."""
        from apps.core.integration import (  # noqa: PLC0415
            register_action_adapter,
            register_subject_recheck,
        )
        from apps.interop import tasks  # noqa: PLC0415
        from apps.interop.webhooks import (  # noqa: PLC0415
            SUBJECT_TYPE,
            WebhookActionAdapter,
            delivery_eligible,
            delivery_lock_key,
        )

        register_action_adapter(WebhookActionAdapter())
        register_subject_recheck(
            SUBJECT_TYPE, delivery_eligible, lock_key=delivery_lock_key
        )
        # Registers the export jobs with Celery's task registry at worker boot.
        assert tasks.build_export  # noqa: S101
        app.conf.beat_schedule["interop-export-dispatch"] = {
            "task": "apps.interop.tasks.dispatch_due_exports",
            "schedule": 300.0,
        }
        app.conf.beat_schedule["interop-export-expiry"] = {
            "task": "apps.interop.tasks.expire_exports",
            "schedule": 900.0,
        }
