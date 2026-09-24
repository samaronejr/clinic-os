"""Display projections of stored payment facts for the staff and patient screens.

Presentation reads committed database state only. It never calls a provider,
never writes and never infers settlement: ``paid`` requires the
database-issued receipt row, so creating a charge, refreshing a page or an
authenticated-but-unverified event can never render as success. Payment
instructions belong to a charge that can still be paid, so a cancelled or
already paid charge carries none on either surface. The copy
vocabulary is closed and carries no clinical text, no settlement evidence
identifier and no provider implementation metadata.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from django.db import connection
from django.utils import timezone

from apps.billing.models import Invoice, PaymentEvent, PixCharge, PixOperation, Receipt
from apps.billing.services import list_invoices, patient_charges, view_invoice
from apps.intake.models import Patient
from apps.tenancy.envelope import reveal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime
    from uuid import UUID

CENTAVOS: Final = 2
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
PNG_HEADER_END: Final = 24
PATIENT_CHARGE_COLUMNS: Final = 13

# One closed state vocabulary for both surfaces. ``open`` is the patient
# ledger's summary of any unsettled charge; the payment screen refines it.
LABELS: Final = {
    "draft": "Rascunho",
    "issued": "Aguardando código",
    "pending": "Aguardando pagamento",
    "expired": "Código expirado",
    "flagged": "Pagamento em verificação",
    "paid": "Pago",
    "cancelled": "Cancelada",
    "open": "Em aberto",
}
TONES: Final = {
    "draft": "muted",
    "issued": "pending",
    "pending": "pending",
    "expired": "error",
    "flagged": "error",
    "paid": "success",
    "cancelled": "muted",
    "open": "pending",
}
STAFF_DETAIL: Final = {
    "draft": (
        "Os valores ainda podem ser revisados. Emita a cobrança para congelá-los."
    ),
    "issued": (
        "Valores congelados. Gere o código de pagamento para enviar ao paciente."
    ),
    "pending": (
        "O código está válido. A cobrança só fica paga depois que o servidor "
        "confirma o pagamento."
    ),
    "expired": (
        "O código expirou sem confirmação. Gere um novo código; a cobrança e o "
        "valor continuam os mesmos."
    ),
    "flagged": (
        "Um aviso de pagamento precisa de conferência manual. Nenhum recibo foi "
        "emitido e nenhum valor foi devolvido."
    ),
    "paid": (
        "Pagamento simulado registrado pelo servidor e recibo de ensaio emitido. "
        "Nenhum dinheiro real foi recebido."
    ),
    "cancelled": ("Cobrança cancelada. Nenhuma devolução é feita por esta tela."),
    "open": "Cobrança em aberto.",
}
PATIENT_DETAIL: Final = {
    "draft": "Esta cobrança ainda não foi emitida pela clínica.",
    "issued": "A clínica ainda não gerou o código de pagamento desta cobrança.",
    "pending": (
        "Use o código abaixo para pagar. O estado muda para pago somente quando "
        "o pagamento é confirmado; atualizar a página não conclui o pagamento."
    ),
    "expired": (
        "O código de pagamento expirou. Peça um novo código à clínica; o valor "
        "da cobrança continua o mesmo."
    ),
    "flagged": (
        "A clínica está conferindo um aviso de pagamento desta cobrança. "
        "Nada foi concluído até a confirmação."
    ),
    "paid": (
        "Pagamento simulado registrado neste ensaio; nenhum dinheiro real foi "
        "cobrado. O recibo de ensaio está abaixo."
    ),
    "cancelled": "Esta cobrança foi cancelada pela clínica.",
    "open": "Cobrança em aberto.",
}
# Staff-facing reasons in plain language: no provider or transport wording.
REASONS: Final = {
    "underpaid": "O valor informado foi menor que o da cobrança.",
    "overpaid": "O valor informado foi maior que o da cobrança.",
    "currency_mismatch": "A moeda informada é diferente da cobrança.",
    "reversed": "O pagamento foi estornado depois de informado.",
    "expired": "A cobrança expirou antes da confirmação.",
    "cancelled": "A cobrança foi cancelada antes da confirmação.",
    "unverified_settlement": (
        "Um aviso de pagamento não pôde ser confirmado na consulta oficial."
    ),
}
DEFAULT_REASON: Final = (
    "O pagamento não foi confirmado e precisa de conferência manual."
)
# A terminal state can no longer become paid on its own, so it neither
# refreshes nor offers payment instructions.
TERMINAL_STATES: Final = frozenset({"draft", "paid", "cancelled"})


def amount_in_reais(amount_minor: int) -> Decimal:
    """Convert stored BRL centavos into an exact decimal for display only."""
    return Decimal(amount_minor).scaleb(-CENTAVOS)


def _png_size(qr_base64: str) -> tuple[int, int]:
    """Read the stored PNG's intrinsic size, or ``(0, 0)`` when unreadable."""
    try:
        raw = base64.b64decode(qr_base64, validate=True)
    except (binascii.Error, ValueError):
        return (0, 0)
    if len(raw) < PNG_HEADER_END or not raw.startswith(PNG_SIGNATURE):
        return (0, 0)
    width, height = struct.unpack(">II", raw[16:PNG_HEADER_END])
    return (int(width), int(height))


@dataclass(frozen=True, slots=True)
class PixInstructions:
    """Exactly the stored copy text and image of one charge request."""

    copy_code: str
    qr_base64: str
    qr_width: int
    qr_height: int
    expires_at: datetime
    expired: bool


@dataclass(frozen=True, slots=True)
class ChargeView:
    """One charge as a screen shows it: money, state and recovery, nothing else."""

    invoice_id: UUID
    reference: UUID
    amount: Decimal
    currency: str
    state: str
    issued_at: datetime | None
    instructions: PixInstructions | None
    receipt_reference: UUID | None
    receipt_issued_at: datetime | None

    @property
    def label(self) -> str:
        """Return the closed-vocabulary label of this state."""
        return LABELS[self.state]

    @property
    def tone(self) -> str:
        """Return the badge tone of this state."""
        return TONES[self.state]

    @property
    def paid(self) -> bool:
        """Report the only success state: a stored receipt exists."""
        return self.state == "paid"

    @property
    def cancelled(self) -> bool:
        """Report that the clinic ended this charge without any payment."""
        return self.state == "cancelled"

    @property
    def live(self) -> bool:
        """Report whether a server-confirmed settlement can still change this."""
        return self.state not in TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class StaffCharge:
    """One clinic charge with the actions its stored state actually allows."""

    charge: ChargeView
    patient_name: str
    patient_id: UUID
    created_at: datetime
    revision: int
    released: bool
    reason: str

    @property
    def detail(self) -> str:
        """Return the staff explanation of the current state."""
        return STAFF_DETAIL[self.charge.state]

    @property
    def can_issue(self) -> bool:
        """Report whether the reviewed draft can still be frozen."""
        return self.charge.state == "draft"

    @property
    def can_cancel(self) -> bool:
        """Report whether an unsettled charge can still be cancelled."""
        return self.charge.state not in ("paid", "cancelled")

    @property
    def can_release(self) -> bool:
        """Report whether an issued charge is not yet visible to its patient."""
        return self.charge.issued_at is not None and not self.released

    @property
    def can_request_code(self) -> bool:
        """Report whether a first payment code can be generated."""
        return self.charge.state == "issued"

    @property
    def can_regenerate(self) -> bool:
        """Report whether the expired request can get a linked successor."""
        return self.charge.state == "expired"

    @property
    def can_confirm(self) -> bool:
        """Report whether a verified settlement can still be attested."""
        return self.charge.state in ("issued", "pending", "expired", "flagged")

    @property
    def actionable(self) -> bool:
        """Report whether this charge still allows any action at all."""
        return any(
            (
                self.can_issue,
                self.can_request_code,
                self.can_regenerate,
                self.can_release,
                self.can_cancel,
            )
        )


@dataclass(frozen=True, slots=True)
class PatientCharge:
    """One charge as its own patient sees it, with the patient explanation."""

    charge: ChargeView
    clinic_timezone: str = ""

    @property
    def detail(self) -> str:
        """Return the patient explanation of the current state."""
        return PATIENT_DETAIL[self.charge.state]


def _instructions(
    operation: PixOperation | None,
    charge: PixCharge | None,
    *,
    now: datetime,
) -> PixInstructions | None:
    if operation is None or charge is None:
        return None
    width, height = _png_size(charge.qr_base64)
    return PixInstructions(
        copy_code=charge.copy_code,
        qr_base64=charge.qr_base64,
        qr_width=width,
        qr_height=height,
        expires_at=operation.expires_at,
        expired=operation.expires_at <= now,
    )


def _state(
    invoice_state: str,
    *,
    receipt: bool,
    instructions: PixInstructions | None,
    flagged: bool,
) -> str:
    """Map stored facts to one display state; only a receipt means paid."""
    if receipt and invoice_state == Invoice.State.PAID:
        return "paid"
    if invoice_state == Invoice.State.CANCELLED:
        return "cancelled"
    if invoice_state == Invoice.State.DRAFT:
        return "draft"
    if flagged:
        return "flagged"
    if instructions is None:
        return "issued"
    return "expired" if instructions.expired else "pending"


def _payable_instructions(
    state: str, instructions: PixInstructions | None
) -> PixInstructions | None:
    """Keep the code and QR only while paying this charge still does anything."""
    return None if state in TERMINAL_STATES else instructions


def _head_operation(operations: Sequence[PixOperation]) -> PixOperation | None:
    """Return the request no successor replaced, i.e. the current one."""
    replaced = {
        operation.previous_id
        for operation in operations
        if operation.previous_id is not None
    }
    current = [operation for operation in operations if operation.pk not in replaced]
    if not current:
        return None
    return max(current, key=lambda operation: (operation.created_at, operation.pk))


def _latest_event(events: Iterable[PaymentEvent]) -> PaymentEvent | None:
    ordered = sorted(events, key=lambda event: (event.received_at, event.pk))
    return ordered[-1] if ordered else None


def _staff_charges(invoices: Sequence[Invoice]) -> tuple[StaffCharge, ...]:
    """Assemble every staff projection with a fixed number of queries."""
    now = timezone.now()
    invoice_ids = [invoice.pk for invoice in invoices]
    operations: dict[UUID, list[PixOperation]] = {}
    for operation in PixOperation.objects.filter(invoice_id__in=invoice_ids):
        operations.setdefault(operation.invoice_id, []).append(operation)
    results = {
        result.operation_id: result
        for result in PixCharge.objects.filter(invoice_id__in=invoice_ids)
    }
    receipts = {
        receipt.invoice_id: receipt
        for receipt in Receipt.objects.filter(invoice_id__in=invoice_ids)
    }
    events: dict[UUID, list[PaymentEvent]] = {}
    for event in PaymentEvent.objects.filter(invoice_id__in=invoice_ids):
        events.setdefault(event.operation_id, []).append(event)
    names = dict(
        Patient.objects.filter(
            pk__in=[invoice.patient_id for invoice in invoices]
        ).values_list("pk", "full_name")
    )
    return tuple(
        _staff_charge(invoice, operations, results, receipts, events, names, now=now)
        for invoice in invoices
    )


def _staff_charge(  # noqa: PLR0913 - one prefetched map per related table
    invoice: Invoice,
    operations: dict[UUID, list[PixOperation]],
    results: dict[UUID, PixCharge],
    receipts: dict[UUID, Receipt],
    events: dict[UUID, list[PaymentEvent]],
    names: dict[UUID, str],
    *,
    now: datetime,
) -> StaffCharge:
    operation = _head_operation(operations.get(invoice.pk, ()))
    result = results.get(operation.pk) if operation is not None else None
    instructions = _instructions(operation, result, now=now)
    receipt = receipts.get(invoice.pk)
    event = (
        _latest_event(events.get(operation.pk, ())) if operation is not None else None
    )
    flagged = event is not None and (
        event.resolution == PaymentEvent.Resolution.OPERATOR_REQUIRED
    )
    state = _state(
        invoice.state,
        receipt=receipt is not None,
        instructions=instructions,
        flagged=flagged,
    )
    return StaffCharge(
        charge=ChargeView(
            invoice_id=invoice.pk,
            reference=invoice.reference,
            amount=amount_in_reais(invoice.amount_minor),
            currency=invoice.currency,
            state=state,
            issued_at=invoice.issued_at,
            instructions=_payable_instructions(state, instructions),
            receipt_reference=receipt.reference if receipt else None,
            receipt_issued_at=receipt.issued_at if receipt else None,
        ),
        patient_name=names.get(invoice.patient_id, ""),
        patient_id=invoice.patient_id,
        created_at=invoice.created_at,
        revision=invoice.revision,
        released=invoice.released_at is not None,
        reason=(
            REASONS.get(event.reason_code or "", DEFAULT_REASON)
            if flagged and event is not None
            else ""
        ),
    )


def current_operation(*, clinic_id: UUID, invoice_id: UUID) -> PixOperation | None:
    """Return the charge request no successor replaced, under staff authority.

    Actions resolve the request to reuse or regenerate from stored rows, so a
    browser can never name the request a retry should act on.
    """
    view_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
    return _head_operation(list(PixOperation.objects.filter(invoice_id=invoice_id)))


def staff_charges(*, clinic_id: UUID) -> tuple[StaffCharge, ...]:
    """List the authorized clinic's charges with their current payment state."""
    return _staff_charges(list_invoices(clinic_id=clinic_id))


def staff_charge(*, clinic_id: UUID, invoice_id: UUID) -> StaffCharge:
    """Project one authorized charge; the service denies every other scope."""
    invoice = view_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
    return _staff_charges((invoice,))[0]


def patient_ledger() -> tuple[PatientCharge, ...]:
    """List the session patient's released charges, without payment instructions."""
    return tuple(
        PatientCharge(
            charge=ChargeView(
                invoice_id=row.invoice_id,
                reference=row.reference,
                amount=amount_in_reais(row.amount_minor),
                currency=row.currency,
                state=(
                    "paid"
                    if row.receipt_reference is not None
                    else ("cancelled" if row.state == "cancelled" else "open")
                ),
                issued_at=row.issued_at,
                instructions=None,
                receipt_reference=row.receipt_reference,
                receipt_issued_at=row.receipt_issued_at,
            )
        )
        for row in patient_charges()
    )


def patient_charge(*, invoice_id: UUID) -> PatientCharge | None:
    """Resolve one released charge for the live patient session, or ``None``.

    The database resolver re-validates the session, the granted ``billing``
    operation and the exact patient scope, so another patient's identifier
    simply resolves to nothing.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.billing_patient_charge(%s)", [invoice_id]
        )
        row = cursor.fetchone()
    if row is None or len(row) != PATIENT_CHARGE_COLUMNS:
        return None
    (
        resolved_id,
        reference,
        amount_minor,
        currency,
        invoice_state,
        issued_at,
        receipt_reference,
        receipt_issued_at,
        copy_code_envelope,
        qr_base64_envelope,
        expires_at,
        flagged,
        clinic_timezone,
    ) = row
    # The resolver returns tenant envelopes; the live patient session
    # authorizes decryption through the protected boundary.
    copy_code = (
        None
        if copy_code_envelope is None
        else reveal(
            purpose="billing.pixcharge.copy_code",
            envelope=bytes(copy_code_envelope),
        ).decode("utf-8")
    )
    qr_base64 = (
        ""
        if qr_base64_envelope is None
        else reveal(
            purpose="billing.pixcharge.qr_base64",
            envelope=bytes(qr_base64_envelope),
        ).decode("utf-8")
    )
    now = timezone.now()
    instructions = (
        PixInstructions(
            copy_code=copy_code,
            qr_base64=qr_base64,
            qr_width=_png_size(qr_base64)[0],
            qr_height=_png_size(qr_base64)[1],
            expires_at=expires_at,
            expired=expires_at <= now,
        )
        if copy_code and expires_at is not None
        else None
    )
    state = _state(
        invoice_state,
        receipt=receipt_reference is not None,
        instructions=instructions,
        flagged=bool(flagged),
    )
    return PatientCharge(
        clinic_timezone=clinic_timezone,
        charge=ChargeView(
            invoice_id=resolved_id,
            reference=reference,
            amount=amount_in_reais(amount_minor),
            currency=currency,
            state=state,
            issued_at=issued_at,
            instructions=_payable_instructions(state, instructions),
            receipt_reference=receipt_reference,
            receipt_issued_at=receipt_issued_at,
        ),
    )
