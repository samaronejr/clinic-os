"""Single-turn, source-cited brief commands on the governed synthetic gateway."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from django.db import transaction

from apps.ai.artifacts import create_input, input_for, read_artifact
from apps.ai.brief_contracts import (
    ChartRequest,
    parse_request,
    question_topic,
    validate_reply,
)
from apps.ai.chart_authority import visit_permission
from apps.ai.context import assemble_context
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Purpose,
    Usage,
)
from apps.ai.models import AIInvocation
from apps.ai.policy import enabled_policy
from apps.ai.services import invoke
from apps.core.chart_sources import ChartBatch, ChartSource
from apps.ehr.chart_sources import chart_enrollment
from apps.identity.current_context import current_actor_id

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.contracts import InvocationResult


@dataclass(frozen=True, slots=True)
class Brief:
    """No unsupported assertion or concealed-record count enters presentation."""

    state: Literal["generating", "ready", "unavailable"]
    invocation_id: UUID | None = None
    answer: Literal["sources", "not_found", "refused"] = "not_found"
    chart: ChartBatch = field(default_factory=lambda: ChartBatch((), ()))


def request_brief(
    *,
    clinic_id: UUID,
    session_binding: str,
    enrollment_id: UUID | None = None,
    encounter_id: UUID | None = None,
    question: str = "",
) -> Brief:
    """Retrieve one permitted chart, then enqueue only immutable cited references."""
    enrolled = chart_enrollment(
        clinic_id=clinic_id, enrollment_id=enrollment_id, encounter_id=encounter_id
    )
    try:
        enabled_policy(Purpose.BRIEF)
        permission = visit_permission(
            clinic_id=clinic_id, enrollment_id=enrolled, encounter_id=encounter_id
        )
        topic = question_topic(question) if question else "brief"
        if topic is None:
            return Brief("ready", answer="refused")
        with transaction.atomic():
            context = assemble_context(clinic_id=clinic_id, enrollment_id=enrolled)
            request = ChartRequest(
                enrolled,
                session_binding,
                topic,
                question,
                tuple(source.reference() for source in context.sources),
                permission,
            )
            result = _admit(clinic_id, request)
            if result.reason is not None:
                transaction.set_rollback(True)
                return Brief("unavailable")
            return Brief("generating", result.invocation_id)
    except GatewayRefusedError:
        return Brief("unavailable")


def _admit(clinic_id: UUID, request: ChartRequest) -> InvocationResult:
    content = request.encode()
    parse_request(content)
    source = create_input(
        clinic_id=clinic_id,
        patient_enrollment_id=request.enrollment,
        purpose=Purpose.BRIEF,
        content=content,
    )
    return invoke(
        clinic_id=clinic_id,
        purpose=Purpose.BRIEF,
        principal=current_actor_id(),
        inputs_ref=source.pk,
        budget=Budget(
            Usage(tokens_in=50_000, tokens_out=50_000), maximum_micros=1_000_000
        ),
    )


def display_brief(
    *,
    clinic_id: UUID,
    session_binding: str,
    invocation_id: UUID,
    enrollment_id: UUID | None = None,
    encounter_id: UUID | None = None,
) -> Brief:
    """Resolve every citation again; revocation hides the item, not its count."""
    enrolled = chart_enrollment(
        clinic_id=clinic_id, enrollment_id=enrollment_id, encounter_id=encounter_id
    )
    invocation = AIInvocation.objects.filter(
        pk=invocation_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        purpose=Purpose.BRIEF.value,
    ).first()
    if invocation is None:
        raise GatewayAccessDeniedError
    try:
        source = input_for(
            clinic_id=clinic_id,
            inputs_ref=invocation.inputs_ref_id,
            purpose=Purpose.BRIEF,
        )
        enabled_policy(Purpose.BRIEF)
        binding = visit_permission(
            clinic_id=clinic_id, enrollment_id=enrolled, encounter_id=encounter_id
        )
    except GatewayRefusedError:
        return Brief("unavailable")
    request = parse_request(source.content)
    if (
        request.enrollment != enrolled
        or request.permission != binding
        or not hmac.compare_digest(request.session, session_binding)
    ):
        raise GatewayAccessDeniedError
    if invocation.status == "reserved":
        return Brief("generating", invocation_id)
    if invocation.status != "succeeded":
        return Brief("unavailable")
    try:
        citations = validate_reply(
            read_artifact(clinic_id=clinic_id, artifact_id=invocation.output.pk),
            request,
        )
    except (GatewayInputError, GatewayRefusedError):
        return Brief("unavailable")
    current = assemble_context(clinic_id=clinic_id, enrollment_id=enrolled)
    sources: tuple[ChartSource, ...] = tuple(
        item for item in current.sources if item.reference() in citations
    )
    return Brief(
        "ready",
        invocation_id,
        "sources" if sources else "not_found",
        ChartBatch(
            sources,
            tuple(
                (section, state)
                for section, state in current.chart.coverage
                if request.topic in {"brief", section}
            ),
        ),
    )
