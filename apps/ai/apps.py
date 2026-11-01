"""Register only offline providers and the shared action outbox boundary."""

from django.apps import AppConfig


class AiConfig(AppConfig):
    """Keep all inference registrations together, without startup DB queries."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai"

    def ready(self) -> None:
        """Use the same startup registration model as other send adapters."""
        from apps.ai.adapters import (  # noqa: PLC0415
            RecordedFixtureProvider,
            SyntheticProvider,
            register_provider,
        )
        from apps.ai.outbox import (  # noqa: PLC0415
            GatewayActionAdapter,
            eligible,
            organization_lock_key,
        )
        from apps.ai.tasks import dispatch  # noqa: PLC0415
        from apps.core.integration import (  # noqa: PLC0415
            register_action_adapter,
            register_action_dispatcher,
            register_subject_recheck,
        )

        register_provider(SyntheticProvider())
        register_provider(RecordedFixtureProvider())
        register_action_adapter(GatewayActionAdapter())
        register_action_dispatcher("ai_gateway", dispatch)
        register_subject_recheck(
            "ai.invocation", eligible, lock_key=organization_lock_key
        )
