"""Django configuration for the comms domain."""

from django.apps import AppConfig


class CommsConfig(AppConfig):
    """Register the comms domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.comms"

    def ready(self) -> None:
        """Register independently gated channels and send-time revocation checks."""
        from apps.comms.channel_adapters import (  # noqa: PLC0415 - app wiring
            EmailReminderAdapter,
            SMSReminderAdapter,
            WhatsAppReminderAdapter,
        )
        from apps.comms.services import (  # noqa: PLC0415 - app wiring
            SUBJECT_TYPE,
            reminder_lock_key,
            reminder_send_eligible,
        )
        from apps.core.integration import (  # noqa: PLC0415 - app wiring
            register_send_adapter,
            register_subject_recheck,
        )

        for adapter in (
            EmailReminderAdapter(),
            SMSReminderAdapter(),
            WhatsAppReminderAdapter(),
        ):
            register_send_adapter(adapter)
        register_subject_recheck(
            SUBJECT_TYPE, reminder_send_eligible, lock_key=reminder_lock_key
        )
