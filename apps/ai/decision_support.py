"""Optional evidence review without diagnosis, clinical writes or communication."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from django.db import transaction

from apps.ai.artifacts import create_input, input_for, read_artifact
from apps.ai.brief_contracts import ChartRequest
from apps.ai.context import assemble_context
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Purpose,
    Usage,
)
from apps.ai.decision_contracts import (
    DecisionRequest,
    parse_decision_request,
    validate_decision_reply,
)
from apps.ai.decision_policy import authorized_catalog
from apps.ai.models import AIInvocation
from apps.ai.services import invoke
from apps.audit.services import record_phase1_event
from apps.consent.services import ai_disclosure_status
from apps.ehr.chart_sources import chart_enrollment
from apps.identity.current_context import current_actor_id

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.reference_catalog import CatalogEntry
    from apps.core.chart_sources import ChartSource


@dataclass(frozen=True, slots=True)
class Consideration:
    """The presenter receives only currently resolvable evidence."""

    kind: str
    fact: ChartSource
    reference: CatalogEntry


@dataclass(frozen=True, slots=True)
class DecisionPanel:
    """An unfinished invocation is unavailable, never a ready clinical result."""

    state: Literal["unavailable", "ready"] = "unavailable"
    can_request: bool = False
    invocation_id: UUID | None = None
    considerations: tuple[Consideration, ...] = ()
    decision: str = ""


def encounter_catalog(
    *, clinic_id: UUID, encounter_id: UUID
) -> tuple[UUID, tuple[CatalogEntry, ...]]:
    """Authorize the encounter before checking optional capability availability."""
    enrolled = chart_enrollment(clinic_id=clinic_id, encounter_id=encounter_id)
    disclosure = ai_disclosure_status(clinic_id=clinic_id, encounter_id=encounter_id)
    if disclosure is None or not disclosure.informed or disclosure.refused:
        raise GatewayRefusedError(reason="disabled")
    return enrolled, authorized_catalog(clinic_id=clinic_id)


def open_decision_support(*, clinic_id: UUID, encounter_id: UUID) -> DecisionPanel:
    """Open without opting in, retrieving chart content or mutating a session."""
    try:
        encounter_catalog(clinic_id=clinic_id, encounter_id=encounter_id)
    except GatewayRefusedError:
        return DecisionPanel()
    return DecisionPanel(can_request=True)


def request_decision_support(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    session_binding: str,
    opt_in: bool = False,
) -> DecisionPanel:
    """Opt-in is per clinician, session and invocation, and resets on every use."""
    chart_enrollment(clinic_id=clinic_id, encounter_id=encounter_id)
    if type(opt_in) is not bool:
        raise GatewayInputError
    if not opt_in:
        return DecisionPanel()
    try:
        # A budget/configuration refusal must also roll back the input artifact,
        # chart-read audit entries and after-commit callbacks, not merely the job.
        with transaction.atomic():
            enrolled, catalog = encounter_catalog(
                clinic_id=clinic_id, encounter_id=encounter_id
            )
            chart = assemble_context(clinic_id=clinic_id, enrollment_id=enrolled)
            request = DecisionRequest(
                encounter_id,
                ChartRequest(
                    enrolled,
                    session_binding,
                    "brief",
                    "",
                    tuple(source.reference() for source in chart.sources),
                ),
                tuple(entry.reference() for entry in catalog),
            )
            parse_decision_request(request.encode())
            artifact = create_input(
                clinic_id=clinic_id,
                patient_enrollment_id=enrolled,
                purpose=Purpose.DECISION_SUPPORT,
                content=request.encode(),
            )
            result = invoke(
                clinic_id=clinic_id,
                purpose=Purpose.DECISION_SUPPORT,
                principal=current_actor_id(),
                inputs_ref=artifact.pk,
                budget=Budget(
                    Usage(tokens_in=50_000, tokens_out=50_000),
                    maximum_micros=1_000_000,
                ),
            )
            if result.reason is not None:
                transaction.set_rollback(True)
                return DecisionPanel()
            return DecisionPanel(invocation_id=result.invocation_id)
    except GatewayRefusedError:
        return DecisionPanel()


def display_decision_support(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    session_binding: str,
    invocation_id: UUID,
) -> DecisionPanel:
    """Re-resolve patient and licensed references under current display authority."""
    chart_enrollment(clinic_id=clinic_id, encounter_id=encounter_id)
    invocation = AIInvocation.objects.filter(
        pk=invocation_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        purpose=Purpose.DECISION_SUPPORT.value,
    ).first()
    if invocation is None:
        raise GatewayAccessDeniedError
    try:
        artifact = input_for(
            clinic_id=clinic_id,
            inputs_ref=invocation.inputs_ref_id,
            purpose=Purpose.DECISION_SUPPORT,
        )
        request = parse_decision_request(artifact.content)
        if request.encounter != encounter_id or not hmac.compare_digest(
            request.chart.session, session_binding
        ):
            raise GatewayAccessDeniedError
        if invocation.status == "reserved":
            return DecisionPanel(invocation_id=invocation_id)
        if invocation.status != "succeeded":
            return DecisionPanel()
        hypotheses = validate_decision_reply(
            read_artifact(clinic_id=clinic_id, artifact_id=invocation.output.pk),
            request,
        )
        catalog = authorized_catalog(clinic_id=clinic_id)
    except (GatewayRefusedError, GatewayInputError):
        return DecisionPanel()
    current = assemble_context(
        clinic_id=clinic_id, enrollment_id=request.chart.enrollment
    )
    considerations = tuple(
        Consideration(hypothesis.kind, source, entry)
        for hypothesis in hypotheses
        for source in current.sources
        if source.reference() == hypothesis.fact
        for entry in catalog
        if entry.reference() == hypothesis.citation
    )
    return DecisionPanel(
        "ready",
        can_request=True,
        invocation_id=invocation_id,
        considerations=considerations,
    )


@transaction.atomic
def record_decision(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    session_binding: str,
    invocation_id: UUID,
    decision: str,
) -> DecisionPanel:
    """Record AI-use provenance only; modifications belong to manual chart editing."""
    if decision not in {"accepted", "modified", "rejected"}:
        raise GatewayInputError
    panel = display_decision_support(
        clinic_id=clinic_id,
        encounter_id=encounter_id,
        session_binding=session_binding,
        invocation_id=invocation_id,
    )
    if panel.state != "ready" or not panel.considerations:
        raise GatewayRefusedError(reason="configuration_unavailable")
    record_phase1_event(
        f"ai.decision_support.{decision}",
        clinic_id=clinic_id,
        affected_record_id=invocation_id,
    )
    return DecisionPanel(
        panel.state, panel.can_request, invocation_id, panel.considerations, decision
    )
