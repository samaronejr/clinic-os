"""Review-only analyst commands on the established governed AI gateway."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from django.db import transaction

from apps.ai.artifacts import (
    ArtifactProjection,
    artifact_content,
    artifact_selector,
    create_input,
    input_for,
    render_artifact,
)
from apps.ai.contracts import (
    Budget,
    GatewayRefusedError,
    Purpose,
    Usage,
)
from apps.ai.effects import check_invocation
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.pending_access import clinic_workflow
from apps.ai.pending_outputs import rows as pending_rows
from apps.ai.policy import check_configuration, enabled_policy
from apps.ai.services import invoke
from apps.analytics.analyst_contracts import (
    AnalystRequest,
    parse_request,
    question_metric,
    validate_reply,
)
from apps.analytics.contracts import (
    AnalyticsAccessDeniedError,
    AnalyticsConflictError,
    AnalyticsInputError,
    Query,
    Result,
)
from apps.analytics.models import AnalystReview
from apps.analytics.services import authorize, refresh_metric, require_snapshot
from apps.audit.services import record_phase1_event

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.pending_outputs import VisiblePending


@dataclass(frozen=True, slots=True)
class Proposal:
    """An explicit human review precedes even this read-only metric execution."""

    state: str
    invocation_id: UUID | None = None
    query: Query | None = None
    authorization_version: str = ""


def request_analyst(*, clinic_id: UUID, question: str, query: Query) -> Proposal:
    """Retrieve an authorized definition; never send or store the raw question."""
    actor, version = authorize(clinic_id=clinic_id)
    selected = question_metric(question)
    if selected is None:
        return Proposal("clarification")
    query = replace(query, metric_id=selected)
    require_snapshot(clinic_id=clinic_id, query=query, authorization_version=version)
    try:
        with transaction.atomic():
            source = create_input(
                clinic_id=clinic_id,
                purpose=Purpose.ANALYST,
                content=AnalystRequest(query, version).encode(),
            )
            result = invoke(
                clinic_id=clinic_id,
                purpose=Purpose.ANALYST,
                principal=actor,
                inputs_ref=source.pk,
                budget=Budget(
                    Usage(tokens_in=2048, tokens_out=1024), maximum_micros=1_000_000
                ),
            )
            if result.reason is not None:
                transaction.set_rollback(True)
                return Proposal("unavailable")
    except GatewayRefusedError:
        return Proposal("unavailable")
    return Proposal("generating", result.invocation_id)


def display_proposal(*, clinic_id: UUID, invocation_id: UUID) -> Proposal:
    """Revalidate requester, expiry, provider policy and authorization version."""
    return render_artifact(
        prepare_proposal(clinic_id=clinic_id, invocation_id=invocation_id),
        clinic_id=clinic_id,
        emit=lambda proposal: proposal,
    )


@clinic_workflow
def prepare_proposal(
    *, clinic_id: UUID, invocation_id: UUID
) -> Proposal | ArtifactProjection[Proposal]:
    """Defer the ready report descriptor until after the final scoped SELECT."""
    actor, version = authorize(clinic_id=clinic_id)
    invocation = AIInvocation.objects.filter(
        pk=invocation_id,
        clinic_id=clinic_id,
        user_id=actor,
        purpose=Purpose.ANALYST.value,
    ).first()
    if invocation is None:
        raise AnalyticsAccessDeniedError
    source = input_for(
        clinic_id=clinic_id,
        inputs_ref=invocation.inputs_ref_id,
        purpose=Purpose.ANALYST,
    )
    requested = parse_request(source.content)
    require_snapshot(
        clinic_id=clinic_id,
        query=requested.query,
        authorization_version=requested.authorization_version,
    )
    try:
        enabled_policy(Purpose.ANALYST)
        check_configuration(invocation.configuration, clinic_id)
    except GatewayRefusedError:
        return Proposal("unavailable", invocation_id)
    if invocation.status == "reserved":
        return Proposal("generating", invocation_id)
    if invocation.status != "succeeded":
        return Proposal("unavailable", invocation_id)
    output = (
        pending_rows(AIArtifact)
        .filter(invocation=invocation, kind="output")
        .values_list("pk", flat=True)
        .first()
    )
    if output is None:
        raise AnalyticsAccessDeniedError
    selector = artifact_selector(clinic_id=clinic_id, artifact_id=output)

    def build(checked: VisiblePending[AIArtifact]) -> Proposal:
        return Proposal(
            "review",
            invocation_id,
            validate_reply(artifact_content(checked), requested),
            version,
        )

    return ArtifactProjection(selector, build, Proposal("unavailable", invocation_id))


@transaction.atomic
@clinic_workflow
def review_proposal(
    *,
    clinic_id: UUID,
    invocation_id: UUID,
    query_digest: str,
    authorization_version: str,
    decision: str,
) -> Result | None:
    """Bind a human decision to exact output bytes and current effective authority."""
    if decision not in {"accepted", "rejected"}:
        raise AnalyticsInputError
    proposal = display_proposal(clinic_id=clinic_id, invocation_id=invocation_id)
    query = proposal.query
    if query is None:
        raise AnalyticsConflictError
    if (
        query.digest != query_digest
        or proposal.authorization_version != authorization_version
    ):
        raise AnalyticsConflictError
    actor, _version = authorize(clinic_id=clinic_id, metric_id=query.metric_id)
    invocation = AIInvocation.objects.select_for_update().get(pk=invocation_id)
    check_invocation(clinic_id=clinic_id, invocation_id=invocation.pk)
    previous = pending_rows(AnalystReview).filter(invocation=invocation).first()
    if previous is not None:
        if previous.decision != decision or previous.descriptor != query.payload():
            raise AnalyticsConflictError
    else:
        reviewed = pending_rows(AnalystReview).create(
            organization_id=invocation.organization_id,
            clinic_id=clinic_id,
            user_id=actor,
            invocation=invocation,
            descriptor=query.payload(),
            window_start=query.start,
            window_end=query.end,
            authorization_version=authorization_version,
            decision=decision,
        )
        record_phase1_event(
            f"analytics.analyst.{decision}",
            clinic_id=clinic_id,
            affected_record_id=reviewed.pk,
        )
    if decision == "rejected":
        return None
    check_invocation(clinic_id=clinic_id, invocation_id=invocation.pk)
    return refresh_metric(clinic_id=clinic_id, query=query, command_id=invocation_id)
