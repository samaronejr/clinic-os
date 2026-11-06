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
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import version_scope
from apps.workflows.care.models import CarePlan

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.ai.action_payload import JSON


def instant_text(value: datetime) -> str:
    """Spell an aware instant exactly as action payload v1 requires."""
    return value.isoformat().replace("+00:00", "Z")


def item_digest(item: CareProposal) -> str:
    """Bind the item's identity, plan revision and exact proposed effect."""
    if item.window_start is None or item.window_end is None:
        raise ActionRefusedError(Refusal.SOURCE)
    return hashlib.sha256(
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


def plan_source(plan: CarePlan) -> dict[str, JSON]:
    """Cite the approved plan version the proposal came from."""
    return {
        "kind": "care_plan",
        "id": str(plan.pk),
        "version": 1,
        "revision": plan.revision,
        "content_sha256": plan.fingerprint,
    }


def item_source(item: CareProposal) -> dict[str, JSON]:
    """Cite the exact reviewed proposal row and its current revision."""
    return {
        "kind": "care_proposal",
        "id": str(item.pk),
        "version": 1,
        "revision": item.revision,
        "content_sha256": item_digest(item),
    }


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


def check_care_sources(*, data: dict[str, JSON], clinic_id: UUID) -> None:
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
    item = CareProposal.objects.filter(
        pk=identifier(cited["care_proposal"]["id"]), clinic_id=clinic_id
    ).first()
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
        or cited["care_proposal"] != item_source(item)
        or care_window(data["parameters"])
        != (item.kind, item.window_start, item.window_end)
    ):
        raise ActionRefusedError(Refusal.SOURCE)
