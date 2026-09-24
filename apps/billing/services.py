"""Clinic-authorized invoice lifecycle and minimal patient charge projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.billing.models import Invoice, Receipt, Settlement
from apps.core.idempotency import create_fingerprint
from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from datetime import datetime

MAX_MINOR: Final = 2**63 - 1


class BillingAccessDeniedError(Exception):
    """Non-identifying denial for unavailable or unauthorized billing records."""


class BillingValueError(ValueError):
    """Reject inexact or unsupported financial input at the service boundary."""


class BillingConflictError(Exception):
    """A stale revision or incompatible lifecycle transition cannot be applied."""


class BillingIdempotencyConflictError(Exception):
    """Reject reuse of one charge-create key for different charge terms."""

    def __init__(self) -> None:
        """Expose one stable non-identifying conflict message."""
        super().__init__("charge creation idempotency conflict")


def _terms(amount_minor: int, currency: str) -> None:
    if type(amount_minor) is not int or not 0 < amount_minor <= MAX_MINOR:
        msg = "amount must be positive integer BRL centavos"
        raise BillingValueError(msg)
    if currency != "BRL":
        msg = "currency must be BRL"
        raise BillingValueError(msg)


def _clinic(clinic_id: UUID) -> tuple[UUID, Clinic]:
    try:
        actor = require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        return actor, Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        msg = "billing access denied"
        raise BillingAccessDeniedError(msg) from error


def _invoice(clinic_id: UUID, invoice_id: UUID, *, lock: bool = False) -> Invoice:
    _, clinic = _clinic(clinic_id)
    rows = Invoice.objects.filter(organization_id=clinic.organization_id, clinic=clinic)
    if lock:
        rows = rows.select_for_update()
    try:
        return rows.get(pk=invoice_id)
    except Invoice.DoesNotExist as error:
        msg = "billing access denied"
        raise BillingAccessDeniedError(msg) from error


def _charge_fingerprint(clinic_id: UUID, patient_id: UUID, amount_minor: int) -> bytes:
    """Hash exactly the charge terms one create submission asked for."""
    return create_fingerprint(
        "invoice",
        {
            "amount_minor": str(amount_minor),
            "clinic_id": str(clinic_id),
            "currency": "BRL",
            "patient_id": str(patient_id),
        },
    )


def _charge_for_key(
    organization_id: UUID, idempotency_key: UUID, fingerprint: bytes
) -> Invoice | None:
    """Return the charge this key already created, or refuse different terms."""
    invoice = Invoice.objects.filter(
        organization_id=organization_id, idempotency_key=idempotency_key
    ).first()
    if invoice is None:
        return None
    if bytes(invoice.create_fingerprint) != fingerprint:
        raise BillingIdempotencyConflictError
    return invoice


def create_invoice(  # noqa: PLR0913 - exact scope, terms, key and optional sources
    *,
    clinic_id: UUID,
    patient_id: UUID,
    amount_minor: int,
    idempotency_key: UUID,
    appointment_id: UUID | None = None,
    encounter_id: UUID | None = None,
) -> Invoice:
    """Create a BRL draft; the database validates enrollment and optional sources.

    The caller names the create operation, and the organization-unique key
    stored with the row is what makes that operation repeatable: replaying the
    exact same submission returns the charge it already created instead of
    opening a second one. The fingerprint covers the clinic, the patient and
    the exact BRL terms, so reusing one key for different terms is refused
    rather than silently resolved to the first charge.
    """
    _terms(amount_minor, "BRL")
    if type(idempotency_key) is not UUID:
        msg = "idempotency key must be an opaque UUID"
        raise BillingValueError(msg)
    fingerprint = _charge_fingerprint(clinic_id, patient_id, amount_minor)
    with transaction.atomic():
        _, clinic = _clinic(clinic_id)
        replay = _charge_for_key(clinic.organization_id, idempotency_key, fingerprint)
        if replay is not None:
            return replay
        try:
            with transaction.atomic():
                return Invoice.objects.create(
                    organization_id=clinic.organization_id,
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    appointment_id=appointment_id,
                    encounter_id=encounter_id,
                    amount_minor=amount_minor,
                    currency="BRL",
                    idempotency_key=idempotency_key,
                    create_fingerprint=fingerprint,
                )
        except IntegrityError:
            # A concurrent identical submission won the unique key; the loser
            # returns that exact charge. Every other refused insert - an
            # unenrolled patient, a foreign source - still raises.
            concurrent = _charge_for_key(
                clinic.organization_id, idempotency_key, fingerprint
            )
            if concurrent is None:
                raise
            return concurrent


def charge_for_operation(*, clinic_id: UUID, idempotency_key: UUID) -> Invoice | None:
    """Return the charge one named create operation already opened, if any.

    Callers that name their own operation use this to tell an opened charge
    from a resolved replay, so a screen can say which of the two happened
    instead of claiming a creation that did not occur.
    """
    _, clinic = _clinic(clinic_id)
    return Invoice.objects.filter(
        organization_id=clinic.organization_id, idempotency_key=idempotency_key
    ).first()


def revise_invoice(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    expected_revision: int,
    amount_minor: int,
    currency: str = "BRL",
) -> Invoice:
    """Retain each draft version and reject stale edits before changing any terms."""
    _terms(amount_minor, currency)
    with transaction.atomic():
        invoice = _invoice(clinic_id, invoice_id, lock=True)
        if (
            invoice.state != Invoice.State.DRAFT
            or invoice.revision != expected_revision
        ):
            msg = "invoice is issued or revision is stale"
            raise BillingConflictError(msg)
        invoice.amount_minor = amount_minor
        invoice.currency = currency
        invoice.revision += 1
        invoice.save(update_fields=("amount_minor", "currency", "revision"))
        return invoice


def issue_invoice(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    expected_revision: int,
) -> Invoice:
    """Freeze exactly the reviewed draft; opening does not release it to a patient."""
    with transaction.atomic():
        invoice = _invoice(clinic_id, invoice_id, lock=True)
        if invoice.revision != expected_revision:
            msg = "invoice revision is stale"
            raise BillingConflictError(msg)
        if invoice.state == Invoice.State.OPEN:
            return invoice
        if invoice.state != Invoice.State.DRAFT:
            msg = "invoice cannot be issued"
            raise BillingConflictError(msg)
        invoice.state = Invoice.State.OPEN
        invoice.issued_at = timezone.now()
        invoice.save(update_fields=("state", "issued_at"))
        return invoice


def release_invoice(*, clinic_id: UUID, invoice_id: UUID) -> Invoice:
    """Explicitly release an issued charge and its eventual receipt to its patient."""
    with transaction.atomic():
        invoice = _invoice(clinic_id, invoice_id, lock=True)
        if invoice.issued_at is None:
            msg = "only issued charges can be released"
            raise BillingConflictError(msg)
        if invoice.released_at is None:
            invoice.released_at = timezone.now()
            invoice.save(update_fields=("released_at",))
        return invoice


def cancel_invoice(*, clinic_id: UUID, invoice_id: UUID) -> Invoice:
    """Cancel an unpaid charge without deleting history or initiating a refund."""
    with transaction.atomic():
        invoice = _invoice(clinic_id, invoice_id, lock=True)
        if invoice.state == Invoice.State.PAID:
            msg = "settled invoices cannot be cancelled"
            raise BillingConflictError(msg)
        if invoice.state != Invoice.State.CANCELLED:
            invoice.state = Invoice.State.CANCELLED
            invoice.save(update_fields=("state",))
        return invoice


def confirm_settlement(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    confirmation_reference: UUID,
    amount_minor: int,
    currency: str = "BRL",
) -> Receipt:
    """Attest a manually verified settlement; never call this from a raw callback.

    The billing actor is responsible for confirming external settlement evidence.
    A caller cannot submit clinical/provider text or claim payment from charge creation.
    Retries of the same exact confirmation converge on the original receipt.
    """
    _terms(amount_minor, currency)
    if type(confirmation_reference) is not UUID:
        msg = "confirmation reference must be an opaque UUID"
        raise BillingValueError(msg)
    with transaction.atomic():
        actor, _ = _clinic(clinic_id)
        invoice = _invoice(clinic_id, invoice_id, lock=True)
        if (invoice.amount_minor, invoice.currency) != (amount_minor, currency):
            msg = "settlement terms mismatch"
            raise BillingConflictError(msg)
        existing = Settlement.objects.filter(invoice=invoice).first()
        if existing is not None:
            if existing.confirmation_reference != confirmation_reference:
                msg = "invoice already settled"
                raise BillingConflictError(msg)
            return Receipt.objects.get(settlement=existing)
        if invoice.state != Invoice.State.OPEN:
            msg = "only open invoices can settle"
            raise BillingConflictError(msg)
        settlement = Settlement.objects.create(
            organization_id=invoice.organization_id,
            invoice=invoice,
            confirmation_reference=confirmation_reference,
            amount_minor=amount_minor,
            currency=currency,
            confirmed_by_id=actor,
        )
        return Receipt.objects.get(settlement=settlement)


def view_invoice(*, clinic_id: UUID, invoice_id: UUID) -> Invoice:
    """Read one exact-clinic charge, never an organization-wide total."""
    return _invoice(clinic_id, invoice_id)


def list_invoices(*, clinic_id: UUID) -> tuple[Invoice, ...]:
    """List only charges in the clinic where the actor has billing authority."""
    _, clinic = _clinic(clinic_id)
    return tuple(Invoice.objects.filter(clinic=clinic).order_by("created_at", "pk"))


@dataclass(frozen=True, slots=True)
class PatientCharge:
    """Public billing fields only: no encounter, staff, or settlement evidence IDs."""

    invoice_id: UUID
    reference: UUID
    amount_minor: int
    currency: str
    state: str
    issued_at: datetime
    receipt_reference: UUID | None
    receipt_issued_at: datetime | None


def patient_charges() -> tuple[PatientCharge, ...]:
    """Resolve own released charges using the live server-bound patient session."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.billing_patient_charges()")
        return tuple(PatientCharge(*row) for row in cursor.fetchall())
