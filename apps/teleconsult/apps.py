"""Django configuration for the teleconsult domain."""

from django.apps import AppConfig


class TeleconsultConfig(AppConfig):
    """Register the teleconsult domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.teleconsult"

    def ready(self) -> None:
        """Bind the synthetic room adapter and its send-time rechecks."""
        from apps.core.integration import (  # noqa: PLC0415 - app wiring
            register_send_adapter,
            register_subject_recheck,
        )
        from apps.teleconsult.adapters import (  # noqa: PLC0415 - app wiring
            PARTICIPANT_SUBJECT,
            SyntheticRoomAdapter,
        )
        from apps.teleconsult.participants import (  # noqa: PLC0415 - app wiring
            participant_revoke_eligible,
        )
        from apps.teleconsult.services import (  # noqa: PLC0415 - app wiring
            SUBJECT_TYPE,
            session_room_eligible,
        )

        register_send_adapter(SyntheticRoomAdapter())
        register_subject_recheck(SUBJECT_TYPE, session_room_eligible)
        register_subject_recheck(PARTICIPANT_SUBJECT, participant_revoke_eligible)
