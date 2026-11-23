"""Protected input/output access under the caller's existing tenant context."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.ai import authorization
from apps.ai.authorization import StaffInput
from apps.ai.contracts import (
    MAX_CONTENT_BYTES,
    GatewayAccessDeniedError,
    GatewayInputError,
    Method,
    Purpose,
)
from apps.ai.effects import check_invocation
from apps.ai.models import AIArtifact
from apps.ai.policy import enabled_policy
from apps.ai.retained_readers import check_retained_input
from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.brief_contracts import ChartDisplay


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
    with transaction.atomic():
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


def input_for(
    *,
    clinic_id: UUID,
    inputs_ref: UUID,
    purpose: Purpose,
    display: ChartDisplay | None = None,
) -> AIArtifact:
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
    if artifact.kind == "output":
        invocation = artifact.invocation
        if invocation is None:
            raise GatewayAccessDeniedError
        check_invocation(clinic_id=clinic_id, invocation_id=invocation.pk)
        source = invocation.inputs_ref
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
    content: str = artifact.content
    return content
