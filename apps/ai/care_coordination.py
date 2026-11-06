"""AI-05 care coordination: typed proposals from an approved plan version.

The plan's clinician requests suggestions over the finalized note's plan
section. The synthetic model output is untrusted; ``validate_reply`` is the
only path to a reviewable :class:`CareProposal`. Administrative items are
approved by staff as a previewed bundle through the todo-39 agent platform
(``care_action.create``, digest-bound, revalidated at execution). Clinical
items can only be accepted by the patient's clinician. A changed plan makes
every pending item stale. Nothing here sends a message or diagnoses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4, uuid5

import rfc8785
from django.db import connection, transaction

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionInputError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import parse_payload
from apps.ai.action_validation import authority_snapshot, database_time
from apps.ai.agent_policies import CARE_TOOLS
from apps.ai.approvals import approve_action
from apps.ai.artifacts import create_input, input_for, read_artifact
from apps.ai.care_contracts import CareRequest, parse_request, validate_reply
from apps.ai.care_models import CareProposal
from apps.ai.care_sources import (
    instant_text,
    item_digest,
    item_source,
    plan_is_current,
    plan_source,
)
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Purpose,
    Usage,
)
from apps.ai.execution import execute_action
from apps.ai.models import AgentPolicyVersion, AIInvocation
from apps.ai.policy import enabled_policy
from apps.ai.proposals import AgentContext, propose_action
from apps.ai.services import invoke
from apps.audit.services import record_phase1_event
from apps.ehr.services import ClinicalAccessDeniedError, view_version
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.providers.services import current_version
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import load_plan, require_clinician
from apps.workflows.care.models import CareAction, CarePlan
from apps.workflows.care.services import CareActionSpec, add_care_action
from apps.workflows.care.vocab import ADMIN_KINDS
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from apps.ai.action_payload import JSON
    from apps.ai.attestations import StepUpEvidence

__all__ = (
    "CareAccessDeniedError",
    "CareBundle",
    "CareConflictError",
    "ItemOutcome",
    "approve_care_bundle",
    "bundle_digest",
    "care_bundle",
    "collect_care_coordination",
    "decide_care_item",
    "pending_bundle",
    "request_care_coordination",
)

BUDGET: Final = Budget(
    Usage(tokens_in=20_000, tokens_out=4_000), maximum_micros=1_000_000
)
PROPOSAL_MINUTES: Final = 10


class CareAccessDeniedError(PermissionError):
    """Unknown, foreign and unauthorized selectors share one denial."""

    def __init__(self) -> None:
        """Disclose no selector, patient or plan."""
        super().__init__("care coordination access denied")


class CareConflictError(RuntimeError):
    """The reviewed bundle, item or plan changed; review it again."""

    def __init__(self) -> None:
        """Carry no record detail."""
        super().__init__("care coordination conflict")


@dataclass(frozen=True, slots=True)
class CareBundle:
    """One model run over one plan; items are only those the caller may see."""

    state: Literal["generating", "ready", "unavailable", "stale"]
    invocation_id: UUID | None = None
    plan_id: UUID | None = None
    items: tuple[CareProposal, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """The per-item result of one bundle approval; no clinical text."""

    item_id: UUID
    state: Literal["approved", "stale", "refused"]
    reason: str = ""


def _deny() -> CareAccessDeniedError:
    return CareAccessDeniedError()


def _care_staff(clinic_id: UUID) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_care_staff(%s)", [clinic_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True


def _plan_text(*, clinic_id: UUID, plan: CarePlan) -> str:
    """Read only the plan section of the finalized source, as its clinician."""
    try:
        version = view_version(clinic_id=clinic_id, version_id=plan.source_version_id)
    except ClinicalAccessDeniedError:
        raise _deny() from None
    content = version.content if isinstance(version.content, dict) else {}
    text = content.get("plan", "")
    return text if isinstance(text, str) else ""


@transaction.atomic
def request_care_coordination(*, clinic_id: UUID, plan_id: UUID) -> CareBundle:
    """Queue one model run over the plan section; only its clinician may ask."""
    try:
        plan = load_plan(clinic_id=clinic_id, plan_id=plan_id)
        require_clinician(clinic_id=clinic_id, enrollment_id=plan.enrollment_id)
    except (WorkflowAccessDeniedError, CurrentActorError):
        raise _deny() from None
    if not plan_is_current(plan):
        raise CareConflictError
    try:
        enabled_policy(Purpose.CARE_COORDINATION)
    except GatewayRefusedError:
        return CareBundle("unavailable", plan_id=plan.pk)
    text = _plan_text(clinic_id=clinic_id, plan=plan)
    request = CareRequest(plan.pk, plan.revision, plan.source_version_id, text)
    try:
        parse_request(request.encode())
    except GatewayInputError:
        return CareBundle("unavailable", plan_id=plan.pk)
    source = create_input(
        clinic_id=clinic_id,
        purpose=Purpose.CARE_COORDINATION,
        patient_enrollment_id=plan.enrollment_id,
        content=request.encode(),
    )
    result = invoke(
        clinic_id=clinic_id,
        purpose=Purpose.CARE_COORDINATION,
        principal=current_actor_id(),
        inputs_ref=source.pk,
        budget=BUDGET,
    )
    if result.invocation_id is None:
        return CareBundle("unavailable", plan_id=plan.pk)
    record_phase1_event(
        "ai.care_coordination.requested",
        clinic_id=clinic_id,
        affected_record_id=result.invocation_id,
    )
    return CareBundle("generating", result.invocation_id, plan.pk)


def _visible_items(clinic_id: UUID, invocation_id: UUID) -> tuple[CareProposal, ...]:
    return tuple(
        CareProposal.objects.filter(clinic_id=clinic_id, invocation_id=invocation_id)
        .select_related("plan")
        .order_by("position")
    )


def _requested(
    *, clinic_id: UUID, invocation_id: UUID
) -> tuple[AIInvocation, CareRequest, CarePlan]:
    """Admit only the requester's own run over a plan they still treat."""
    invocation = AIInvocation.objects.filter(
        pk=invocation_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        purpose=Purpose.CARE_COORDINATION.value,
    ).first()
    if invocation is None:
        raise _deny()
    try:
        source = input_for(
            clinic_id=clinic_id,
            inputs_ref=invocation.inputs_ref_id,
            purpose=Purpose.CARE_COORDINATION,
        )
        request = parse_request(source.content)
        plan = CarePlan.objects.filter(pk=request.plan_id, clinic_id=clinic_id).first()
        if plan is None:
            raise _deny()
        require_clinician(clinic_id=clinic_id, enrollment_id=plan.enrollment_id)
    except (GatewayAccessDeniedError, GatewayInputError, CurrentActorError):
        raise _deny() from None
    return invocation, request, plan


def _store(
    *, invocation: AIInvocation, plan: CarePlan, output: str
) -> tuple[CareProposal, ...]:
    """Persist every model item as a pending proposal or a closed refusal."""
    accepted, refused = validate_reply(output)
    anchor = plan.created_at
    rows: list[tuple[int, dict[str, object]]] = [
        (
            item.position,
            {
                "kind": item.kind,
                "audience": item.audience,
                "state": "pending",
                "window_start": anchor + timedelta(days=item.start_days),
                "window_end": anchor + timedelta(days=item.end_days),
            },
        )
        for item in accepted
    ]
    rows += [
        (
            item.position,
            {
                "kind": item.kind,
                # Row security hides every refusal from staff, whatever its kind.
                "audience": "administrative"
                if item.kind in ADMIN_KINDS
                else "clinical",
                "state": "refused",
                "reason_code": item.reason,
            },
        )
        for item in refused
    ]
    for position, terms in sorted(rows, key=lambda row: row[0]):
        proposal = CareProposal.objects.create(
            organization_id=plan.organization_id,
            clinic_id=plan.clinic_id,
            invocation=invocation,
            plan=plan,
            enrollment_id=plan.enrollment_id,
            source_version_id=plan.source_version_id,
            plan_revision=plan.revision,
            requested_by_id=invocation.user_id,
            position=position,
            **terms,
        )
        record_phase1_event(
            "ai.care_coordination.proposed"
            if proposal.state == "pending"
            else "ai.care_coordination.refused",
            clinic_id=plan.clinic_id,
            affected_record_id=proposal.pk,
        )
    return _visible_items(plan.clinic_id, invocation.pk)


@transaction.atomic
def collect_care_coordination(*, clinic_id: UUID, invocation_id: UUID) -> CareBundle:
    """Turn the requester's finished run into reviewable proposals, once.

    Every model item is judged on its own: refused items are kept as refusals
    with a closed reason and no model text; valid ones become pending
    proposals. A replay returns the rows already created.
    """
    invocation, request, plan = _requested(
        clinic_id=clinic_id, invocation_id=invocation_id
    )
    existing = _visible_items(clinic_id, invocation.pk)
    if existing:
        return CareBundle("ready", invocation.pk, plan.pk, existing)
    if invocation.status == "reserved":
        return CareBundle("generating", invocation.pk, plan.pk)
    if invocation.status != "succeeded":
        return CareBundle("unavailable", invocation.pk, plan.pk)
    if plan.revision != request.plan_revision or not plan_is_current(plan):
        return CareBundle("stale", invocation.pk, plan.pk)
    try:
        output = read_artifact(clinic_id=clinic_id, artifact_id=invocation.output.pk)
        items = _store(invocation=invocation, plan=plan, output=output)
    except (GatewayAccessDeniedError, GatewayInputError):
        return CareBundle("unavailable", invocation.pk, plan.pk)
    return CareBundle("ready", invocation.pk, plan.pk, items)


def care_bundle(*, clinic_id: UUID, invocation_id: UUID) -> CareBundle:
    """Return the bundle rows the caller may see; none at all is the denial."""
    items = _visible_items(clinic_id, invocation_id)
    if not items:
        raise _deny()
    return CareBundle("ready", invocation_id, items[0].plan_id, items)


def bundle_digest(
    *, clinic_id: UUID, invocation_id: UUID, items: Iterable[CareProposal]
) -> str:
    """Bind the exact previewed administrative effects, in position order."""
    return hashlib.sha256(
        rfc8785.dumps(
            {
                "schema": "care-bundle-v1",
                "clinic": str(clinic_id),
                "invocation": str(invocation_id),
                "items": [item_digest(item) for item in items],
            }
        )
    ).hexdigest()


def pending_bundle(*, clinic_id: UUID, invocation_id: UUID) -> tuple[CareProposal, ...]:
    """Lock the pending administrative items a staff approver may preview."""
    if not _care_staff(clinic_id):
        raise _deny()
    return tuple(
        CareProposal.objects.select_for_update()
        .filter(
            clinic_id=clinic_id,
            invocation_id=invocation_id,
            audience="administrative",
            state="pending",
        )
        .select_related("plan")
        .order_by("position")
    )


def _decide(item: CareProposal, state: str, **links: object) -> CareProposal:
    item.state = state
    item.decided_by_id = current_actor_id()
    item.decided_at = database_time()
    item.revision += 1
    for name, value in links.items():
        setattr(item, name, value)
    item.save(
        update_fields=(
            "state",
            "reason_code",
            "action_proposal",
            "care_action",
            "decided_by",
            "decided_at",
            "revision",
        )
    )
    record_phase1_event(
        f"ai.care_coordination.{state}",
        clinic_id=item.clinic_id,
        affected_record_id=item.pk,
    )
    return item


def _stale(item: CareProposal) -> CareProposal:
    item.reason_code = "source_superseded"
    return _decide(item, "stale")


def _care_policy(clinic_id: UUID) -> AgentPolicyVersion:
    policy = (
        AgentPolicyVersion.objects.filter(
            clinic_id=clinic_id, capability="care_coordination"
        )
        .order_by("-version")
        .first()
    )
    if (
        policy is None
        or not policy.enabled
        or policy.autonomy_ceiling != "A1"
        or set(policy.tool_allowlist) != set(CARE_TOOLS)
    ):
        raise ActionRefusedError(Refusal.DISABLED)
    return policy


def _payload(*, item: CareProposal, policy: AgentPolicyVersion, attempt: UUID) -> str:
    """Build the closed action envelope for one exact administrative effect."""
    provider = current_version("email", clinic_id=item.clinic_id)
    if provider is None or item.window_start is None or item.window_end is None:
        raise ActionRefusedError(Refusal.POLICY)
    now = database_time()
    sources = sorted((plan_source(item.plan), item_source(item)), key=rfc8785.dumps)
    data: dict[str, JSON] = {
        "schema_version": 1,
        "action_id": str(uuid4()),
        "operation": {"name": "care_action.create", "version": 1},
        "organization_id": str(item.organization_id),
        "clinic_id": str(item.clinic_id),
        "patient_id": str(item.enrollment_id),
        "subject": {
            "kind": "care_plan",
            "id": str(item.plan_id),
            "expected_state": "active",
            "revision": item.plan.revision,
        },
        "source_versions": list(sources),
        "document": None,
        "recipients": [],
        "amounts": {
            "currency": "BRL",
            "centavos": "0",
            "direction": "none",
            "allocations": [],
        },
        "provider": {
            "capability_version": str(provider.pk),
            "account": provider.account,
            "environment": provider.environment,
            "api_version": provider.api_version,
        },
        "parameters": {
            "kind": item.kind,
            "window_start": instant_text(item.window_start),
            "window_end": instant_text(item.window_end),
        },
        "not_before": instant_text(now - timedelta(seconds=1)),
        "expires_at": instant_text(now + timedelta(minutes=PROPOSAL_MINUTES)),
        "approver_authority_snapshots": [
            authority_snapshot(clinic_id=item.clinic_id, permission="tasks.assign")
        ],
        "approval_policy": {
            "policy_id": str(policy.pk),
            "version": policy.version,
            "quorum": 1,
            "autonomy": "A1",
        },
        "compensation_policy": policy.compensation_policy,
        "idempotency_key": str(uuid5(item.pk, f"care-bundle:{attempt}")),
    }
    return rfc8785.dumps(data).decode()


def _approve_item(
    *,
    item: CareProposal,
    policy: AgentPolicyVersion,
    attempt: UUID,
    step_up: Callable[[str], StepUpEvidence],
) -> ItemOutcome:
    if item.plan.revision != item.plan_revision or not plan_is_current(item.plan):
        _stale(item)
        return ItemOutcome(item.pk, "stale", Refusal.SOURCE.value)
    payload = parse_payload(_payload(item=item, policy=policy, attempt=attempt))
    proposal = propose_action(
        clinic_id=item.clinic_id, payload=payload, context=AgentContext()
    )
    if proposal.state != "validated":
        return ItemOutcome(item.pk, "refused", proposal.reason_code)
    approve_action(
        clinic_id=item.clinic_id,
        proposal_id=proposal.pk,
        expected_digest=proposal.digest,
        evidence=step_up(proposal.digest),
    )
    result = execute_action(clinic_id=item.clinic_id, proposal_id=proposal.pk)
    if result.state != "receipt":
        reason = result.reason.value if result.reason else Refusal.STATE.value
        if result.reason is Refusal.SOURCE:
            _stale(item)
            return ItemOutcome(item.pk, "stale", reason)
        return ItemOutcome(item.pk, "refused", reason)
    action = CareAction.objects.get(
        clinic_id=item.clinic_id, idempotency_key=uuid5(item.pk, "care-action")
    )
    _decide(item, "approved", action_proposal=proposal, care_action=action)
    return ItemOutcome(item.pk, "approved")


@transaction.atomic
def approve_care_bundle(
    *,
    clinic_id: UUID,
    invocation_id: UUID,
    digest: str,
    step_up: Callable[[str], StepUpEvidence],
) -> tuple[ItemOutcome, ...]:
    """Approve exactly the previewed administrative bundle, item by item.

    ``digest`` must equal the preview's :func:`bundle_digest`; any change in
    the pending items refuses the whole bundle before any effect. ``step_up``
    mints a server receipt for one exact proposal digest from the request's
    current TOTP verification, so each effect keeps its own approval. A stale
    or refused item never blocks the valid ones.
    """
    items = pending_bundle(clinic_id=clinic_id, invocation_id=invocation_id)
    if not items or digest != bundle_digest(
        clinic_id=clinic_id, invocation_id=invocation_id, items=items
    ):
        raise CareConflictError
    policy = _care_policy(clinic_id)
    attempt = uuid4()
    outcomes: list[ItemOutcome] = []
    for item in items:
        try:
            with transaction.atomic():
                outcomes.append(
                    _approve_item(
                        item=item, policy=policy, attempt=attempt, step_up=step_up
                    )
                )
        except ActionRefusedError as error:
            outcomes.append(ItemOutcome(item.pk, "refused", error.reason.value))
        except (ActionAccessDeniedError, ActionInputError):
            outcomes.append(ItemOutcome(item.pk, "refused", Refusal.AUTHORITY.value))
    return tuple(outcomes)


def _decider(*, clinic_id: UUID, item: CareProposal, decision: str) -> None:
    """Admit the patient's clinician; staff may only decline administrative work."""
    try:
        require_clinician(clinic_id=clinic_id, enrollment_id=item.enrollment_id)
    except CurrentActorError:
        if (
            decision == "accept"
            or item.audience != "administrative"
            or not _care_staff(clinic_id)
        ):
            raise _deny() from None


def _accept(*, clinic_id: UUID, item: CareProposal) -> CareProposal:
    """Add the clinical action through the clinician's own care authority."""
    if item.audience != "clinical":
        raise CareConflictError
    if (
        item.plan.revision != item.plan_revision
        or not plan_is_current(item.plan)
        or item.window_start is None
        or item.window_end is None
    ):
        return _stale(item)
    try:
        action = add_care_action(
            clinic_id=clinic_id,
            plan_id=item.plan_id,
            spec=CareActionSpec(
                kind=item.kind,
                window_start=item.window_start,
                window_end=item.window_end,
            ),
            idempotency_key=uuid5(item.pk, "care-action"),
        )
    except (WorkflowAccessDeniedError, CurrentActorError):
        raise _deny() from None
    except (WorkflowConflictError, WorkflowInputError):
        raise CareConflictError from None
    return _decide(item, "accepted", care_action=action)


@transaction.atomic
def decide_care_item(
    *,
    clinic_id: UUID,
    item_id: UUID,
    decision: Literal["accept", "reject"],
    expected_revision: int,
) -> CareProposal:
    """Accept or decline one pending item under the decider's own authority."""
    if decision not in ("accept", "reject") or type(expected_revision) is not int:
        raise CareConflictError
    item = (
        CareProposal.objects.select_for_update()
        .filter(pk=item_id, clinic_id=clinic_id)
        .select_related("plan")
        .first()
    )
    if item is None:
        raise _deny()
    _decider(clinic_id=clinic_id, item=item, decision=decision)
    if item.revision != expected_revision or item.state != "pending":
        raise CareConflictError
    if decision == "reject":
        return _decide(item, "rejected")
    return _accept(clinic_id=clinic_id, item=item)
