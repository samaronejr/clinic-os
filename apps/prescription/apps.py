"""Django configuration for the prescription domain."""

from django.apps import AppConfig


class PrescriptionConfig(AppConfig):
    """Register the prescription domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.prescription"

    def ready(self) -> None:
        """Register the delivery adapter and its send-time recheck."""
        from apps.core.integration import (  # noqa: PLC0415 - app wiring
            register_send_adapter,
            register_subject_recheck,
        )
        from apps.prescription.delivery import (  # noqa: PLC0415 - app wiring
            DocumentDeliveryAdapter,
        )
        from apps.prescription.verification import (  # noqa: PLC0415
            DELIVERY_SUBJECT_TYPE,
            document_delivery_eligible,
            document_delivery_lock_key,
        )

        register_send_adapter(DocumentDeliveryAdapter())
        register_subject_recheck(
            DELIVERY_SUBJECT_TYPE,
            document_delivery_eligible,
            lock_key=document_delivery_lock_key,
        )
