"""Workflow app registration."""

from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    """Register reference-only operational workflows."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workflows"

    def ready(self) -> None:
        """Register the reviewed external action after models are available."""
        from apps.workflows.external import register_adapters  # noqa: PLC0415

        register_adapters()
