"""Register the synthetic-first media adapter and patient context-switch fence."""

from django.apps import AppConfig


class ScribeConfig(AppConfig):
    """Capture adds no startup queries and grants no AI execution authority."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scribe"

    def ready(self) -> None:
        """Register the existing outbox and shell hooks additively."""
        from apps.core.integration import (  # noqa: PLC0415
            register_action_adapter,
            register_action_dispatcher,
            register_subject_recheck,
        )
        from apps.core.patient_context import (  # noqa: PLC0415
            register_context_switch_guard,
        )
        from apps.scribe.outbox import (  # noqa: PLC0415
            MediaActionAdapter,
            consent_lock_key,
            eligible,
        )
        from apps.scribe.switches import CaptureSwitchGuard  # noqa: PLC0415
        from apps.scribe.tasks import dispatch  # noqa: PLC0415

        register_action_adapter(MediaActionAdapter())
        register_action_dispatcher("scribe_media", dispatch)
        register_subject_recheck("scribe.chunk", eligible, lock_key=consent_lock_key)
        register_context_switch_guard("scribe.capture", CaptureSwitchGuard())
