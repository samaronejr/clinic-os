"""Django configuration for the ehr domain."""

from django.apps import AppConfig


class EhrConfig(AppConfig):
    """Register the ehr domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ehr"

    def ready(self) -> None:
        """Wake the durable projector only after source transactions commit."""
        from django.db.models.signals import post_save  # noqa: PLC0415

        from apps.ehr import models  # noqa: PLC0415
        from apps.ehr.tasks import wake_projection  # noqa: PLC0415

        for model in (
            models.MedicationStatement,
            models.Observation,
            models.Encounter,
            models.ClinicalDocumentVersion,
            models.EncounterAddendum,
            models.EpisodeEncounter,
        ):
            post_save.connect(
                wake_projection,
                sender=model,
                dispatch_uid=f"ehr.timeline.wake.{model.__name__}",
            )
