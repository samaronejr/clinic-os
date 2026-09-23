"""Django configuration for the teleconsult domain."""

from django.apps import AppConfig


class TeleconsultConfig(AppConfig):
    """Register the teleconsult domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.teleconsult"

    def ready(self) -> None:
        """Bind the synthetic room adapter and send-time session recheck."""
        from apps.core.integration import (  # noqa: PLC0415 - app wiring
            register_send_adapter,
            register_subject_recheck,
        )
        from apps.teleconsult.adapters import (  # noqa: PLC0415 - app wiring
            SyntheticRoomAdapter,
        )
        from apps.teleconsult.services import (  # noqa: PLC0415 - app wiring
            SUBJECT_TYPE,
            session_room_eligible,
        )

        register_send_adapter(SyntheticRoomAdapter())
        register_subject_recheck(SUBJECT_TYPE, session_room_eligible)
