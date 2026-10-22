"""Explicit operator resolution, clinician acknowledgment and audience release."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.orders.access import current_version, require_version
from apps.orders.ingestion import verify_match
from apps.orders.models import (
    ResultAcknowledgment,
    ResultQuarantine,
    ResultRejection,
    ResultRelease,
)
from apps.orders.values import (
    DISPOSITIONS,
    INVALID,
    REJECTION_CODES,
    OrdersAccessDeniedError,
    OrdersConflictError,
)

if TYPE_CHECKING:
    from uuid import UUID


@transaction.atomic
def match_result(*, clinic_id: UUID, version_id: UUID, order_id: UUID) -> str:
    """Operator evidence cannot override the immutable source's identity factors."""
    try:
        require_permission("order.route", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    current_version(clinic_id=clinic_id, version_id=version_id)
    state = verify_match(clinic_id=clinic_id, version_id=version_id, order_id=order_id)
    if state != "matched":
        ambiguous = ResultQuarantine.objects.filter(
            version_id=version_id, reason="ambiguous"
        ).exists()
        raise OrdersConflictError("match_ambiguous" if ambiguous else "quarantined")
    return state


@transaction.atomic
def reject_result(
    *, clinic_id: UUID, version_id: UUID, evidence_code: str
) -> ResultRejection:
    """Reject a source with fixed operator evidence; no identity text enters audit."""
    try:
        actor = require_permission("order.route", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    row = current_version(clinic_id=clinic_id, version_id=version_id)
    if evidence_code not in REJECTION_CODES:
        raise ValidationError(INVALID)
    receipt, created = ResultRejection.objects.get_or_create(
        version=row,
        defaults={
            "organization_id": row.organization_id,
            "clinic_id": clinic_id,
            "operator_id": actor,
            "evidence_code": evidence_code,
        },
    )
    if receipt.evidence_code != evidence_code:
        raise OrdersConflictError
    if created:
        record_phase1_event(
            "orders.result.rejected", clinic_id=clinic_id, affected_record_id=row.pk
        )
    return receipt


@transaction.atomic
def acknowledge_result(
    *, clinic_id: UUID, version_id: UUID, disposition: str
) -> ResultAcknowledgment:
    """Fence clinical acknowledgment against a concurrent source correction."""
    row = current_version(clinic_id=clinic_id, version_id=version_id)
    require_version(
        clinic_id=clinic_id, version_id=version_id, permission="result.acknowledge"
    )
    if disposition not in DISPOSITIONS:
        raise ValidationError(INVALID)
    actor = require_permission(
        "result.acknowledge",
        clinic_id=clinic_id,
        patient_enrollment_id=row.resultmatch.order.enrollment_id,
    )
    receipt, created = ResultAcknowledgment.objects.get_or_create(
        version=row,
        defaults={
            "organization_id": row.organization_id,
            "clinic_id": clinic_id,
            "reviewer_id": actor,
            "disposition": disposition,
        },
    )
    if receipt.disposition != disposition:
        raise OrdersConflictError
    if created:
        record_phase1_event(
            "orders.result.acknowledged", clinic_id=clinic_id, affected_record_id=row.pk
        )
    return receipt


@transaction.atomic
def release_result(
    *,
    clinic_id: UUID,
    version_id: UUID,
    audience: str,
    delegate_grant_id: UUID | None = None,
) -> ResultRelease:
    """Only an acknowledged current source can be released to an explicit audience."""
    row = current_version(clinic_id=clinic_id, version_id=version_id)
    matched = require_version(
        clinic_id=clinic_id, version_id=version_id, permission="result.release"
    )
    actor = require_permission(
        "result.release",
        clinic_id=clinic_id,
        patient_enrollment_id=matched.order.enrollment_id,
    )
    acknowledgment = ResultAcknowledgment.objects.filter(version=row).first()
    if acknowledgment is None:
        raise OrdersConflictError(code="acknowledgment_required")
    if (audience, delegate_grant_id is None) not in {
        ("patient", True),
        ("delegate", False),
    }:
        raise ValidationError(INVALID)
    if delegate_grant_id is not None:
        from django.db import connection  # noqa: PLC0415

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.orders_delegate_allowed(%s,%s)",
                [version_id, delegate_grant_id],
            )
            if cursor.fetchone() != (True,):
                raise OrdersAccessDeniedError
    receipt, created = ResultRelease.objects.get_or_create(
        version=row,
        audience=audience,
        delegate_grant_id=delegate_grant_id,
        defaults={
            "organization_id": row.organization_id,
            "clinic_id": clinic_id,
            "released_by_id": actor,
            "acknowledgment": acknowledgment,
        },
    )
    if created:
        record_phase1_event(
            "orders.result.released", clinic_id=clinic_id, affected_record_id=row.pk
        )
    return receipt
