"""The existing evaluation and change ledger owns every exposure permission."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from django.db import connection, transaction

from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.models import ModelChangeRequest
from apps.audit.services import record_phase1_event

if TYPE_CHECKING:
    from uuid import UUID

STAGES = {
    "offline": ("shadow", "none"),
    "shadow": ("reviewed_live", "human_review"),
    "reviewed_live": ("monitored_autonomy", "bounded_admin"),
}


def rollout_status(*, clinic_id: UUID, change_id: UUID) -> str:
    """Retained kill generations cannot be undone by reenabling a switch."""
    from apps.ai.evaluation.services import evaluation_staff  # noqa: PLC0415

    evaluation_staff(clinic_id=clinic_id, mode="owner")
    change = (
        ModelChangeRequest.objects.filter(pk=change_id, clinic_id=clinic_id)
        .select_related("configuration", "run")
        .first()
    )
    if change is None:
        raise GatewayAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_rollout_status_for(%s,%s)", [clinic_id, change.pk]
        )
        row = cursor.fetchone()
    status = str(row[0]) if row else "invalidated"
    if (
        status in {"shadow", "reviewed_live", "monitored_autonomy"}
        and candidate_versions(change.configuration) != change.run.versions
    ):
        return "invalidated"
    return status


def autonomy_allowed(*, clinic_id: UUID, configuration_id: UUID) -> bool:
    """Reviewed inference alone never grants administrative autonomy."""
    from apps.ai.policy import rollout_approval  # noqa: PLC0415

    try:
        rollout_approval(
            clinic_id=clinic_id, configuration_id=configuration_id, autonomy=True
        )
    except GatewayRefusedError:
        return False
    return True


@transaction.atomic
def stop_rollout(
    *, clinic_id: UUID, change_id: UUID, state: Literal["invalidated", "killed"]
) -> ModelChangeRequest:
    """Stop an approved stage without rewriting its evidence or approval."""
    from apps.ai.evaluation.services import evaluation_staff  # noqa: PLC0415

    evaluation_staff(clinic_id=clinic_id, mode="owner")
    change = ModelChangeRequest.objects.filter(
        pk=change_id, clinic_id=clinic_id
    ).first()
    if change is None:
        raise GatewayAccessDeniedError
    if state not in {"invalidated", "killed"}:
        raise GatewayRefusedError(reason="configuration_unavailable")
    change.status = state
    change.save(update_fields=("status",))
    record_phase1_event(
        f"ai.rollout.{state}", clinic_id=clinic_id, affected_record_id=change.pk
    )
    return change
