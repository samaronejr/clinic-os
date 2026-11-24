"""Protected input/output access under the caller's existing tenant context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.ai import authorization
from apps.ai.authorization import StaffInput
from apps.ai.brief_contracts import parse_request
from apps.ai.care_contracts import parse_request as parse_care
from apps.ai.contracts import (
    MAX_CONTENT_BYTES,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Method,
    Purpose,
)
from apps.ai.decision_contracts import parse_decision_request
from apps.ai.effects import check_invocation
from apps.ai.models import AIArtifact
from apps.ai.pending_outputs import PendingKey, ai_pending_visible, withheld_artifact
from apps.ai.pending_outputs import rows as pending_rows
from apps.ai.policy import enabled_policy
from apps.ai.retained_readers import check_retained_input
from apps.ai.revenue_sources import parse_context
from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.ai.brief_contracts import ChartDisplay
    from apps.ai.pending_outputs import RenderSelection, VisiblePending


def create_input(
    *,
    clinic_id: UUID,
    purpose: Purpose,
    content: str,
    patient_enrollment_id: UUID | None = None,
    method: Method = "complete",
) -> AIArtifact:
    """Store immutable, purpose-bound synthetic source material as ciphertext."""
    if (
        type(content) is not str
        or not content
        or len(content.encode("utf-8")) > MAX_CONTENT_BYTES
        or method not in {"complete", "stream", "transcribe", "extract"}
        or (purpose is Purpose.SCRIBE_ASR and method != "transcribe")
        or (purpose is Purpose.EXTRACTION and method != "extract")
        or (
            purpose in {Purpose.DECISION_SUPPORT, Purpose.REVENUE_CYCLE, Purpose.BRIEF}
            and method != "complete"
        )
    ):
        raise GatewayInputError
    user_id = authorization.authorize_capability(
        purpose, StaffInput(clinic_id, patient_enrollment_id, content)
    )
    enabled_policy(purpose)
    clinic = Clinic.objects.get(pk=clinic_id)
    source_ref, source_acceptance = _read_origin(purpose, content)
    with transaction.atomic():
        artifact = pending_rows(AIArtifact).create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            user_id=user_id,
            purpose=purpose.value,
            patient_enrollment_id=patient_enrollment_id,
            source_ref=source_ref,
            source_acceptance=source_acceptance,
            method=method,
            content=content,
            retention_class="transient_input",
            expires_at=timezone.now() + timedelta(days=1),
        )
        record_phase1_event(
            "ai.artifact.created", clinic_id=clinic_id, affected_record_id=artifact.pk
        )
    return artifact


def _read_origin(purpose: Purpose, content: str) -> tuple[UUID | None, UUID | None]:
    if purpose is Purpose.CARE_COORDINATION:
        return parse_care(content).plan_id, None
    if purpose is Purpose.REVENUE_CYCLE:
        return parse_context(content).guide_id, None
    if purpose in (Purpose.BRIEF, Purpose.DECISION_SUPPORT):
        chart = (
            parse_request(content)
            if purpose is Purpose.BRIEF
            else parse_decision_request(content).chart
        )
        if chart.permission is not None:
            return chart.permission.encounter, chart.permission.acceptance
    return None, None


def input_for(
    *,
    clinic_id: UUID,
    inputs_ref: UUID,
    purpose: Purpose,
    display: ChartDisplay | None = None,
) -> AIArtifact:
    """Unknown, expired, foreign-clinic, and foreign-user refs all deny equally."""
    artifact = (
        pending_rows(AIArtifact)
        .filter(
            pk=inputs_ref,
            clinic_id=clinic_id,
            user_id=current_actor_id(),
            kind="input",
            purpose=purpose.value,
            expires_at__gt=timezone.now(),
            purged_at__isnull=True,
        )
        .first()
    )
    if artifact is None:
        if withheld_artifact(clinic_id, inputs_ref, purpose, display):
            raise GatewayRefusedError(reason="authority_revoked")
        raise GatewayAccessDeniedError
    if (
        display is not None
        and display.enrollment is not None
        and (display.enrollment != artifact.patient_enrollment_id)
    ):
        raise GatewayAccessDeniedError
    authorization.authorize_capability(
        purpose,
        StaffInput(
            clinic_id,
            artifact.patient_enrollment_id,
            artifact.content,
            display,
            generated_at=artifact.created_at,
            retained_input=artifact.pk,
        ),
    )
    if purpose is Purpose.REVENUE_CYCLE:
        enabled_policy(purpose)
    return artifact


def artifact_selector(*, clinic_id: UUID, artifact_id: UUID) -> PendingKey[AIArtifact]:
    """Check purpose and source without retaining the display payload."""
    artifact = (
        pending_rows(AIArtifact)
        .filter(
            pk=artifact_id,
            clinic_id=clinic_id,
            user_id=current_actor_id(),
            expires_at__gt=timezone.now(),
            purged_at__isnull=True,
        )
        .defer("content")
        .first()
    )
    if artifact is None:
        if withheld_artifact(clinic_id, artifact_id):
            raise GatewayRefusedError(reason="authority_revoked")
        raise GatewayAccessDeniedError
    if artifact.kind == "output":
        invocation = artifact.invocation
        if invocation is None:
            raise GatewayAccessDeniedError
        check_invocation(clinic_id=clinic_id, invocation_id=invocation.pk)
        # The input can be withheld after the output was selected; that is a
        # revoked authority, not an ORM error.
        retained = (
            pending_rows(AIArtifact)
            .filter(pk=invocation.inputs_ref_id, clinic_id=clinic_id)
            .first()
        )
        if retained is None:
            raise GatewayRefusedError(reason="authority_revoked")
        source = retained
    else:
        source = artifact
    authorization.authorize_capability(
        Purpose(artifact.purpose),
        StaffInput(
            clinic_id,
            artifact.patient_enrollment_id,
            source.content,
            generated_at=source.created_at,
            retained_input=source.pk,
        ),
    )
    check_retained_input(source)
    if artifact.purpose == Purpose.REVENUE_CYCLE.value:
        enabled_policy(Purpose.REVENUE_CYCLE)
    return PendingKey(AIArtifact, artifact.pk)


def artifact_content(checked: VisiblePending[AIArtifact]) -> str:
    """Read only the fresh ciphertext projection, never the initial instance."""
    current = checked.record
    if current.expires_at <= timezone.now() or current.purged_at is not None:
        raise GatewayAccessDeniedError
    content: str = current.content
    return content


def read_artifact(*, clinic_id: UUID, artifact_id: UUID) -> str:
    """Reveal content only to its still-authorized requester, before expiry."""
    selector = artifact_selector(clinic_id=clinic_id, artifact_id=artifact_id)

    def project(batch: RenderSelection) -> str:
        checked = batch.get(selector)
        if checked is None:
            raise GatewayRefusedError(reason="authority_revoked")
        return artifact_content(checked)

    return ai_pending_visible((selector,), project, clinic_id=clinic_id)


@dataclass(frozen=True, slots=True)
class ArtifactProjection[V]:
    """Defer building a ready DTO until its artifact is selected for this response."""

    selector: PendingKey[AIArtifact]
    build: Callable[[VisiblePending[AIArtifact]], V]
    unavailable: V


def render_artifact[V, R](
    prepared: V | ArtifactProjection[V],
    *,
    clinic_id: UUID,
    emit: Callable[[V], R],
) -> R:
    """Render a ready DTO in the issuing frame or use its manual fallback."""
    if not isinstance(prepared, ArtifactProjection):
        return emit(prepared)

    def project(batch: RenderSelection) -> R:
        checked = batch.get(prepared.selector)
        if checked is None:
            return emit(prepared.unavailable)
        try:
            dto = prepared.build(checked)
        except GatewayRefusedError:
            dto = prepared.unavailable
        return emit(dto)

    return ai_pending_visible((prepared.selector,), project, clinic_id=clinic_id)
