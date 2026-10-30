"""Workflow app registration."""

from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    """Register reference-only operational workflows."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workflows"

    def ready(self) -> None:
        """Register the external action, the inbox task feed and Today's hint."""
        from apps.workflows import task_feed, today  # noqa: PLC0415
        from apps.workflows.care import tasks as care_tasks  # noqa: PLC0415
        from apps.workflows.external import register_adapters  # noqa: PLC0415

        register_adapters()
        # Registers the care scanner with Celery's task registry at worker boot.
        assert care_tasks.scan_care_job  # noqa: S101
        task_feed.register()
        today.register()
