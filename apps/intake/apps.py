"""Django configuration for the intake domain."""

from django.apps import AppConfig


class IntakeConfig(AppConfig):
    """Register the intake domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.intake"

    def ready(self) -> None:
        """Bind the preference send-time recheck and lock key to the boundary."""
        from apps.core.integration import register_subject_recheck  # noqa: PLC0415
        from apps.intake.contacts import (  # noqa: PLC0415 - app-ready wiring
            SUBJECT_TYPE_PREFERENCE,
            preference_lock_key,
            preference_send_eligible,
        )

        register_subject_recheck(
            SUBJECT_TYPE_PREFERENCE,
            preference_send_eligible,
            lock_key=preference_lock_key,
        )
