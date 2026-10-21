"""Place immutable catalog orders and append legal collection transitions."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import uuid5

import rfc8785
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.orders.access import lock_series
from apps.orders.models import LabAccount, Order, OrderEvent, OrderItem
from apps.orders.values import (
    CATALOG,
    INVALID,
    OrdersAccessDeniedError,
    OrdersConflictError,
)

if TYPE_CHECKING:
    from uuid import UUID


@transaction.atomic
def place_order(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    exam_codes: tuple[str, ...],
    command_id: UUID,
    lab_account_id: UUID | None = None,
) -> Order:
    """Commit the draft and placement under the current professional's authority."""
    try:
        actor = require_permission(
            "order.place", clinic_id=clinic_id, patient_enrollment_id=enrollment_id
        )
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    enrollment = PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).first()
    if enrollment is None:
        raise OrdersAccessDeniedError
    if (
        not exam_codes
        or len(set(exam_codes)) != len(exam_codes)
        or any(code not in CATALOG for code in exam_codes)
    ):
        raise ValidationError(INVALID)
    if (
        lab_account_id is not None
        and not LabAccount.objects.filter(
            pk=lab_account_id, clinic_id=clinic_id, active=True
        ).exists()
    ):
        raise OrdersAccessDeniedError
    fingerprint = hashlib.sha256(
        rfc8785.dumps(
            {
                "enrollment": str(enrollment_id),
                "actor": str(actor),
                "codes": sorted(exam_codes),
                "lab": str(lab_account_id) if lab_account_id else None,
            }
        )
    ).hexdigest()
    lock_series(identifier=command_id)
    existing = Order.objects.filter(clinic_id=clinic_id, command_id=command_id).first()
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise OrdersConflictError
        return existing
    order = Order.objects.create(
        clinic_id=clinic_id,
        organization_id=enrollment.organization_id,
        enrollment_id=enrollment_id,
        ordered_by_id=actor,
        command_id=command_id,
        fingerprint=fingerprint,
        lab_account_id=lab_account_id,
    )
    for code in exam_codes:
        OrderItem.objects.create(
            organization_id=order.organization_id,
            clinic_id=clinic_id,
            order=order,
            exam_code=code,
        )
    for sequence, state in enumerate(("draft", "placed"), 1):
        OrderEvent.objects.create(
            organization_id=order.organization_id,
            clinic_id=clinic_id,
            order=order,
            sequence=sequence,
            state=state,
            actor_id=actor,
            command_id=uuid5(command_id, state),
        )
        record_phase1_event(
            f"orders.order.{state}", clinic_id=clinic_id, affected_record_id=order.pk
        )
    return order


@transaction.atomic
def progress_order(
    *,
    clinic_id: UUID,
    order_id: UUID,
    expected_sequence: int,
    state: str,
    command_id: UUID,
) -> OrderEvent:
    """Progress collection metadata independently of review and release."""
    order = Order.objects.filter(pk=order_id, clinic_id=clinic_id).first()
    if order is None:
        raise OrdersAccessDeniedError
    try:
        actor = require_permission(
            "order.place",
            clinic_id=clinic_id,
            patient_enrollment_id=order.enrollment_id,
        )
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    lock_series(identifier=order_id)
    old = OrderEvent.objects.filter(order=order).order_by("-sequence").first()
    replay = OrderEvent.objects.filter(order=order, command_id=command_id).first()
    if replay is not None:
        if replay.sequence != expected_sequence + 1 or replay.state != state:
            raise OrdersConflictError
        return replay
    if (
        old is None
        or old.sequence != expected_sequence
        or (old.state, state)
        not in {
            ("placed", "collected"),
            ("placed", "in_process"),
            ("collected", "in_process"),
        }
    ):
        raise OrdersConflictError(code="illegal_transition")
    event = OrderEvent.objects.create(
        organization_id=order.organization_id,
        clinic_id=clinic_id,
        order=order,
        sequence=old.sequence + 1,
        state=state,
        actor_id=actor,
        command_id=command_id,
    )
    record_phase1_event(
        f"orders.order.{state}", clinic_id=clinic_id, affected_record_id=order.pk
    )
    return event
