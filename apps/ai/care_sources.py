"""Current-source facts for care-coordination actions (scenario 8 variant).

A ``care_action.create`` payload cites the care plan revision and the exact
pending proposal it executes. Both are reloaded at approval and again at the
send boundary: a superseded plan, a changed revision, a source note that is no
longer the finalized version, or a proposal already decided is a stale source.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import rfc8785

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_payload import care_window, closed, identifier
from apps.ai.care_models import CareProposal
from apps.ai.contracts import GatewayRefusedError
from apps.ai.effects import check_invocation
from apps.ai.pending_choice import pending_choice_allows
from apps.ai.pending_outputs import ai_pending_visible, key
from apps.ai.pending_outputs import rows as pending_rows
from apps.consent.services import ai_disclosure_status
from apps.identity.current_context import CurrentActorError
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import version_scope
from apps.workflows.care.models import CarePlan

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.ai.action_payload import JSON
    from apps.ai.pending_outputs import PendingKey, RenderSelection, VisiblePending


def instant_text(value: datetime) -> str:
    """Spell an aware instant exactly as action payload v1 requires."""
    return value.isoformat().replace("+00:00", "Z")


def checked_item_source(checked: VisiblePending[CareProposal]) -> dict[str, JSON]:
    """Bind a checked item's identity, plan revision and exact proposed effect."""
    item = checked.record
    if item.window_start is None or item.window_end is None:
        raise ActionRefusedError(Refusal.SOURCE)
    digest = hashlib.sha256(
        rfc8785.dumps(
            {
                "id": str(item.pk),
                "invocation": str(item.invocation_id),
                "plan": str(item.plan_id),
                "plan_revision": item.plan_revision,
                "position": item.position,
                "kind": item.kind,
                "window_start": instant_text(item.window_start),
                "window_end": instant_text(item.window_end),
            }
        )
    ).hexdigest()
    return {
        "kind": "care_proposal",
        "id": str(item.pk),
        "version": 1,
        "revision": item.revision,
        "content_sha256": digest,
    }


def plan_source(plan: CarePlan) -> dict[str, JSON]:
    """Cite the approved plan version the proposal came from."""
    return {
        "kind": "care_plan",
        "id": str(plan.pk),
        "version": 1,
        "revision": plan.revision,
        "content_sha256": plan.fingerprint,
    }


def item_source(
    selector: PendingKey[CareProposal], *, clinic_id: UUID
) -> dict[str, JSON]:
    """Action-time recheck, independent of the reviewer's earlier response."""

    def source(batch: RenderSelection) -> dict[str, JSON]:
        checked = batch.get(selector)
        if checked is None:
            raise ActionRefusedError(Refusal.SOURCE)
        return checked_item_source(checked)

    return ai_pending_visible((selector,), source, clinic_id=clinic_id)


def plan_is_current(plan: CarePlan) -> bool:
    """Active plan whose source note is still the finalized version."""
    if plan.state != "active":
        return False
    try:
        _encounter, _enrollment, state = version_scope(
            clinic_id=plan.clinic_id, version_id=plan.source_version_id
        )
    except WorkflowAccessDeniedError:
        return False
    return state == "finalized"


def care_choice_allows(plan: CarePlan, *, generated_at: datetime | None) -> bool:
    """Bind both choices to the plan, even for a nonclinical staff projection."""
    if not pending_choice_allows(
        clinic_id=plan.clinic_id,
        enrollment_id=plan.enrollment_id,
        generated_at=generated_at,
    ):
        return False
    try:
        disclosure = ai_disclosure_status(
            clinic_id=plan.clinic_id, encounter_id=plan.encounter_id
        )
    except CurrentActorError:
        return False
    # An absent visit attestation does not block the previously authorized
    # administrative plan workflow. An explicit refusal on its own visit does.
    return disclosure is None or (disclosure.informed and not disclosure.refused)


def check_care_sources(*, data: dict[str, JSON], clinic_id: UUID) -> UUID:
    """Reload the plan and the proposal; any drift is a stale source."""
    subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
    sources = data["source_versions"]
    if subject["kind"] != "care_plan" or not isinstance(sources, list):
        raise ActionRefusedError(Refusal.SOURCE)
    cited: dict[str, dict[str, JSON]] = {}
    for raw in sources:
        row = closed(raw, {"kind", "id", "version", "revision", "content_sha256"})
        kind = row["kind"]
        if not isinstance(kind, str) or kind in cited:
            raise ActionRefusedError(Refusal.SOURCE)
        cited[kind] = row
    if set(cited) != {"care_plan", "care_proposal"}:
        raise ActionRefusedError(Refusal.SOURCE)
    plan = CarePlan.objects.filter(
        pk=identifier(subject["id"]), clinic_id=clinic_id
    ).first()
    item = (
        pending_rows(CareProposal)
        .filter(pk=identifier(cited["care_proposal"]["id"]), clinic_id=clinic_id)
        .first()
    )
    if (
        plan is None
        or item is None
        or not plan_is_current(plan)
        or subject
        != {
            "kind": "care_plan",
            "id": str(plan.pk),
            "expected_state": "active",
            "revision": plan.revision,
        }
        or cited["care_plan"] != plan_source(plan)
        or data["patient_id"] != str(plan.enrollment_id)
        or item.plan_id != plan.pk
        or item.plan_revision != plan.revision
        or item.state != "pending"
        or item.audience != "administrative"
        or not care_choice_allows(plan, generated_at=item.created_at)
        or cited["care_proposal"] != item_source(key(item), clinic_id=clinic_id)
        or care_window(data["parameters"])
        != (item.kind, item.window_start, item.window_end)
    ):
        raise ActionRefusedError(Refusal.SOURCE)
    try:
        check_invocation(clinic_id=clinic_id, invocation_id=item.invocation_id)
    except GatewayRefusedError:
        raise ActionRefusedError(Refusal.SOURCE) from None
    return item.invocation_id
