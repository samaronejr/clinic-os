"""Django configuration for the retention domain."""

from config.celery import app
from django.apps import AppConfig


class RetentionConfig(AppConfig):
    """Register the retention domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.retention"

    def ready(self) -> None:
        """Register the retained scanner without altering shared worker settings."""
        app.conf.beat_schedule["retention-policy-sweep"] = {
            "task": "apps.retention.tasks.scan_due",
            "schedule": 60.0,
        }
