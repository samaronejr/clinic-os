"""AI-06: source-backed explanations and human-approved administrative drafts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4, uuid5

import rfc8785
from django.db import connection, transaction
from django.utils.translation import gettext as _

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_payload import JSON, closed, identifier, integer, parse_payload
from apps.ai.action_validation import authority_snapshot, database_time
from apps.ai.agent_policies import REVENUE_TOOLS
from apps.ai.artifacts import create_input, input_for, read_artifact
from apps.ai.care_sources import instant_text
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Purpose,
    Usage,
)
from apps.ai.models import (
    ActionExecution,
    ActionProposal,
    AgentPolicyVersion,
    AIArtifact,
    AIInvocation,
)
from apps.ai.proposals import propose_action
from apps.ai.revenue_contracts import KINDS, validate_reply
from apps.ai.revenue_sources import (
    RevenueContext,
    recheck_context,
    require_finance,
    retrieve_context,
)
from apps.ai.services import invoke
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.models import Clinic
from apps.insurance.revenue import draft_appeal
from apps.providers.services import current_version

if TYPE_CHECKING:
    from apps.ai.action_payload import ActionPayload

BUDGET = Budget(Usage(tokens_in=20_000, tokens_out=8_000), maximum_micros=1_000_000)


@dataclass(frozen=True, slots=True)
class RevenueItem:
    """Displayed prose is rendered from sources, never copied from model text."""

    position: int
    kind: str
    text: str = field(repr=False)
    citations: tuple[dict[str, JSON], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class RevenueReview:
    """Manual work stays available while AI is unavailable, generating or stale."""

    state: Literal["generating", "ready", "unavailable", "stale"]
    invocation_id: UUID | None = None
    output_id: UUID | None = None
    items: tuple[RevenueItem, ...] = ()


@transaction.atomic
def request_revenue_cycle(*, clinic_id: UUID, guide_id: UUID) -> RevenueReview:
    """Queue minimum financial facts; no payer prose, patient identity or chart."""
    require_finance(clinic_id=clinic_id)
    clinic = Clinic.objects.only("organization_id").get(pk=clinic_id)
    with connection.cursor() as cursor:
        # Lock order: gateway budget, agent send, insurance, journal. invoke()
        # takes this budget lock again; policy changes take it before agent send.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-budget:{clinic.organization_id}"],
        )
    context = retrieve_context(clinic_id=clinic_id, guide_id=guide_id)
    try:
        source = create_input(
            clinic_id=clinic_id, purpose=Purpose.REVENUE_CYCLE, content=context.encode()
        )
        result = invoke(
            clinic_id=clinic_id,
            purpose=Purpose.REVENUE_CYCLE,
            principal=current_actor_id(),
            inputs_ref=source.pk,
            budget=BUDGET,
        )
    except GatewayRefusedError:
        return RevenueReview("unavailable")
    return RevenueReview(
        "generating" if result.invocation_id else "unavailable", result.invocation_id
    )


def _render(item: dict[str, JSON], context: RevenueContext) -> str:
    code = item["finding"]
    if item["kind"] == "appeal.draft":
        procedure = closed(
            item["procedure"], {"table", "code", "quantity", "unit_minor"}
        )
        citations = item["citations"]
        if not isinstance(citations, list):
            raise GatewayInputError
        source = next(
            row for row in context.facts.returns if row["source"] in citations
        )
        return _(
            "Administrative appeal draft. Please review the recorded denial. "
            "The submitted procedure is table %(table)s, code %(code)s, "
            "quantity %(quantity)s, unit value %(unit)s centavos. "
            "The payer recorded %(denied)s centavos denied, reason %(reason)s. "
            "These are recorded billing facts, not new clinical assertions. "
            "Clinical supporting evidence was not retrieved; a person must "
            "review it before any submission."
        ) % {
            "table": procedure["table"],
            "code": procedure["code"],
            "quantity": procedure["quantity"],
            "unit": procedure["unit_minor"],
            "denied": source["denied_minor"],
            "reason": source["code"],
        }
    labels = {
        "recognition_evidence_missing": _(
            "No current service-recognition entry was found in the supplied "
            "journal facts."
        ),
        "claim_journal_mismatch": _(
            "The submitted claim total differs from the frozen net journal terms."
        ),
        "return_evidence_missing": _(
            "No payer return was found in the supplied claim sources. "
            "This does not establish payment or denial."
        ),
        "claim_denied": _(
            "The payer return records a denied amount. "
            "A person must review the source and any appeal."
        ),
        "payment_evidence_missing": _(
            "The accepted claim amount exceeds independently posted allocations. "
            "A return is not payment evidence."
        ),
        "payment_claim_mismatch": _(
            "Independently posted allocations exceed the accepted amount in "
            "this return. Review the sources; no refund or adjustment is inferred."
        ),
    }
    if not isinstance(code, str) or code not in labels:
        raise GatewayInputError
    return labels[code]


def _ready(
    *, clinic_id: UUID, output_id: UUID
) -> tuple[AIArtifact, RevenueContext, tuple[dict[str, JSON], ...]]:
    require_finance(clinic_id=clinic_id)
    output = (
        AIArtifact.objects.select_related("invocation")
        .filter(
            pk=output_id,
            clinic_id=clinic_id,
            user_id=current_actor_id(),
            purpose=Purpose.REVENUE_CYCLE.value,
            kind="output",
            expires_at__gt=database_time(),
            purged_at__isnull=True,
        )
        .first()
    )
    if (
        output is None
        or output.invocation is None
        or output.invocation.status != "succeeded"
    ):
        raise GatewayAccessDeniedError
    source = input_for(
        clinic_id=clinic_id,
        inputs_ref=output.invocation.inputs_ref_id,
        purpose=Purpose.REVENUE_CYCLE,
    )
    context = recheck_context(clinic_id=clinic_id, content=source.content)
    reply = read_artifact(clinic_id=clinic_id, artifact_id=output.pk)
    return output, context, validate_reply(reply, context.facts)


@transaction.atomic
def collect_revenue_cycle(*, clinic_id: UUID, invocation_id: UUID) -> RevenueReview:
    """Display only the requester's still-authorized current facts and citations."""
    require_finance(clinic_id=clinic_id)
    invocation = AIInvocation.objects.filter(
        pk=invocation_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        purpose=Purpose.REVENUE_CYCLE.value,
    ).first()
    if invocation is None:
        raise GatewayAccessDeniedError
    if invocation.status != "succeeded":
        return RevenueReview(
            "generating" if invocation.status == "reserved" else "unavailable",
            invocation_id,
        )
    output = AIArtifact.objects.filter(
        clinic_id=clinic_id, invocation=invocation, kind="output"
    ).first()
    if output is None:
        return RevenueReview("unavailable", invocation_id)
    try:
        output, context, items = _ready(clinic_id=clinic_id, output_id=output.pk)
    except GatewayAccessDeniedError:
        return RevenueReview("stale", invocation_id)
    displayed: list[RevenueItem] = []
    for position, item in enumerate(items):
        aliases = item["citations"]
        if not isinstance(aliases, list):
            raise GatewayInputError
        displayed.append(
            RevenueItem(
                position,
                str(item["kind"]),
                _render(item, context),
                tuple(
                    source for source in context.sources if source["alias"] in aliases
                ),
            )
        )
    return RevenueReview("ready", invocation_id, output.pk, tuple(displayed))


def _reference(output: AIArtifact) -> dict[str, JSON]:
    return {
        "kind": "ai_artifact",
        "id": str(output.pk),
        "version": 1,
        "revision": 1,
        "content_sha256": hashlib.sha256(output.content.encode()).hexdigest(),
    }


@transaction.atomic
def propose_revenue_action(
    *, clinic_id: UUID, output_id: UUID, position: int
) -> ActionProposal:
    """Create an exact A1 preview; no insurance or billing command executes here."""
    require_finance(clinic_id=clinic_id)
    with connection.cursor() as cursor:
        # Approval and execution also take the agent send lock before sources.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-send-v1',0))"
        )
    output, _context, items = _ready(clinic_id=clinic_id, output_id=output_id)
    if type(position) is not int or not 0 <= position < len(items):
        raise GatewayInputError
    policy = (
        AgentPolicyVersion.objects.filter(
            clinic_id=clinic_id, capability=Purpose.REVENUE_CYCLE.value
        )
        .order_by("-version")
        .first()
    )
    if (
        policy is None
        or not policy.enabled
        or policy.autonomy_ceiling != "A1"
        or not set(policy.tool_allowlist) <= REVENUE_TOOLS
        or items[position]["kind"] not in policy.tool_allowlist
    ):
        raise ActionRefusedError(Refusal.DISABLED)
    key = uuid5(output.pk, f"revenue:{position}")
    previous = ActionProposal.objects.filter(
        clinic_id=clinic_id, idempotency_key=key
    ).first()
    if previous is not None:
        return previous
    provider = current_version("email", clinic_id=clinic_id)
    if provider is None:
        raise ActionRefusedError(Refusal.POLICY)
    now = database_time()
    payload = {
        "schema_version": 1,
        "action_id": str(uuid4()),
        "operation": {"name": items[position]["kind"], "version": 1},
        "organization_id": str(output.organization_id),
        "clinic_id": str(clinic_id),
        "patient_id": None,
        "subject": {
            "kind": "ai_artifact",
            "id": str(output.pk),
            "expected_state": "output",
            "revision": 1,
        },
        "source_versions": [_reference(output)],
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
        "parameters": {"position": position},
        "not_before": instant_text(now - timedelta(seconds=1)),
        "expires_at": instant_text(min(output.expires_at, now + timedelta(minutes=10))),
        "approver_authority_snapshots": [
            authority_snapshot(clinic_id=clinic_id, permission="tiss.manage")
        ],
        "approval_policy": {
            "policy_id": str(policy.pk),
            "version": policy.version,
            "quorum": 1,
            "autonomy": "A1",
        },
        "compensation_policy": policy.compensation_policy,
        "idempotency_key": str(key),
    }
    return propose_action(
        clinic_id=clinic_id, payload=parse_payload(rfc8785.dumps(payload).decode())
    )


def check_revenue_sources(
    *, clinic_id: UUID, data: dict[str, JSON]
) -> tuple[AIArtifact, RevenueContext, dict[str, JSON]]:
    """Shared by admission, human approval and execution under the source locks."""
    try:
        # Every path converges here, including the generic agent queue that
        # automation staff who also hold Finance can use. PostgreSQL enforces
        # the same rule through ai.0011 (restrictive policies and vote guard).
        require_permission("automation.finance", clinic_id=clinic_id)
    except CurrentActorError:
        raise ActionRefusedError(Refusal.AUTHORITY) from None
    try:
        subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
        output, context, items = _ready(
            clinic_id=clinic_id, output_id=identifier(subject["id"])
        )
        position = integer(
            closed(data["parameters"], {"position"})["position"],
            minimum=0,
            maximum=len(items) - 1,
        )
        item = items[position]
        if (
            subject
            != {
                "kind": "ai_artifact",
                "id": str(output.pk),
                "expected_state": "output",
                "revision": 1,
            }
            or data["source_versions"] != [_reference(output)]
            or closed(data["operation"], {"name", "version"})
            != {"name": item["kind"], "version": 1}
            or item["kind"] not in KINDS
        ):
            raise ActionRefusedError(Refusal.SOURCE)
    except (GatewayAccessDeniedError, GatewayInputError):
        raise ActionRefusedError(Refusal.SOURCE) from None
    return output, context, item


def execute_revenue_draft(*, clinic_id: UUID, payload: ActionPayload) -> UUID:
    """Only the shared execution platform calls this after exact human approval."""
    from apps.ai.approvals import verify_approvals  # noqa: PLC0415

    proposal = ActionProposal.objects.filter(
        pk=identifier(payload.data()["action_id"]),
        clinic_id=clinic_id,
        digest=payload.digest,
        state="executing",
    ).first()
    if (
        proposal is None
        or not ActionExecution.objects.filter(
            proposal=proposal,
            state="executing",
            digest=payload.digest,
            send_started_at__isnull=False,
        ).exists()
    ):
        raise ActionRefusedError(Refusal.QUORUM)
    verify_approvals(proposal=proposal, payload=payload, using="default")
    output, context, item = check_revenue_sources(
        clinic_id=clinic_id, data=payload.data()
    )
    if item["kind"] == "discrepancy.explain":
        return output.pk
    aliases = item["citations"]
    if not isinstance(aliases, list):
        raise ActionRefusedError(Refusal.SOURCE)
    source = next(
        row
        for row in context.sources
        if row["kind"] == "glosa" and row["alias"] in aliases
    )
    # Payer-facing wording only: exact source ids and digests stay in the
    # digest-bound output artifact and on the reviewer page.
    body = _render(item, context)
    glosa_id = identifier(source["id"])
    appeal = draft_appeal(
        clinic_id=clinic_id,
        glosa_id=glosa_id,
        body=body,
        clinical_assertions="",
        command_id=uuid5(glosa_id, hashlib.sha256(body.encode()).hexdigest()),
    )
    return appeal.pk
