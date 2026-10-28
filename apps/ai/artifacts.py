"""Protected input/output access under the caller's existing tenant context."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, assert_never

from django.db import transaction
from django.utils import timezone

from apps.ai.contracts import (
    MAX_CONTENT_BYTES,
    GatewayAccessDeniedError,
    GatewayInputError,
    Method,
    Purpose,
)
from apps.ai.models import AIArtifact
from apps.ai.policy import enabled_policy
from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id, require_permission
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID


def check_authority(
    *, clinic_id: UUID, purpose: Purpose, patient_enrollment_id: UUID | None
) -> UUID:
    """Recheck authority; service principals never borrow a staff identity."""
    match purpose:
        case Purpose.FRONT_DESK:
            permission = "appointment.read"
        case Purpose.ANALYST:
            permission = "charge.read"
        case (
            Purpose.SCRIBE_ASR
            | Purpose.SCRIBE_DRAFT
            | Purpose.BRIEF
            | Purpose.EXTRACTION
            | Purpose.DECISION_SUPPORT
        ):
            permission = "clinical.read"
        case unreachable:
            assert_never(unreachable)
    return require_permission(
        permission, clinic_id=clinic_id, patient_enrollment_id=patient_enrollment_id
    )


@transaction.atomic
def create_input(
    *,
    clinic_id: UUID,
    purpose: Purpose,
    content: str,
    patient_enrollment_id: UUID | None = None,
    method: Method = "complete",
) -> AIArtifact:
    """Store immutable, purpose-bound synthetic source material as ciphertext."""
    user_id = check_authority(
        clinic_id=clinic_id,
        purpose=purpose,
        patient_enrollment_id=patient_enrollment_id,
    )
    enabled_policy(purpose)
    if (
        type(content) is not str
        or not content
        or len(content.encode("utf-8")) > MAX_CONTENT_BYTES
        or method not in {"complete", "stream", "transcribe", "extract"}
        or (purpose is Purpose.SCRIBE_ASR and method != "transcribe")
        or (purpose is Purpose.EXTRACTION and method != "extract")
    ):
        raise GatewayInputError
    clinic = Clinic.objects.get(pk=clinic_id)
    artifact = AIArtifact.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        user_id=user_id,
        purpose=purpose.value,
        patient_enrollment_id=patient_enrollment_id,
        method=method,
        content=content,
        retention_class="transient_input",
        expires_at=timezone.now() + timedelta(days=1),
    )
    record_phase1_event(
        "ai.artifact.created", clinic_id=clinic_id, affected_record_id=artifact.pk
    )
    return artifact


def input_for(*, clinic_id: UUID, inputs_ref: UUID, purpose: Purpose) -> AIArtifact:
    """Unknown, expired, foreign-clinic, and foreign-user refs all deny equally."""
    artifact = AIArtifact.objects.filter(
        pk=inputs_ref,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        kind="input",
        purpose=purpose.value,
        expires_at__gt=timezone.now(),
        purged_at__isnull=True,
    ).first()
    if artifact is None:
        raise GatewayAccessDeniedError
    check_authority(
        clinic_id=clinic_id,
        purpose=purpose,
        patient_enrollment_id=artifact.patient_enrollment_id,
    )
    return artifact


def read_artifact(*, clinic_id: UUID, artifact_id: UUID) -> str:
    """Reveal content only to its still-authorized requester, before expiry."""
    artifact = AIArtifact.objects.filter(
        pk=artifact_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        expires_at__gt=timezone.now(),
        purged_at__isnull=True,
    ).first()
    if artifact is None:
        raise GatewayAccessDeniedError
    check_authority(
        clinic_id=clinic_id,
        purpose=Purpose(artifact.purpose),
        patient_enrollment_id=artifact.patient_enrollment_id,
    )
    content: str = artifact.content
    return content
