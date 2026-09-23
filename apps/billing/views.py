"""Payment screens: stored state only, explicit actions, no inferred settlement.

Every state-changing action is a POST that redirects to its safe GET, so a
refresh, a back navigation or a retried submit re-reads state instead of
creating a second charge. The create operation is named by the server from the
signed-in session and the exact submitted terms, never by a value minted per
rendered page: a form the browser restored on Back therefore recomputes the
same organization-unique name and resolves to the charge it already opened.
Opening a genuinely second identical charge stays possible as an explicit act
that names the charge it repeats. The status region refreshes through
bounded HTMX polling that re-reads the same stored facts; it can never report
success before the database holds the settlement receipt. Provider work never
runs here: the request already owns a tenant transaction, and reconciliation
is a separate authenticated boundary.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from django.contrib import messages
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.billing.adapters import PixUnavailableError
from apps.billing.forms import ChargeForm, SettlementForm
from apps.billing.pix import complete_pix_charge, prepare_pix_charge
from apps.billing.presentation import (
    StaffCharge,
    current_operation,
    patient_charge,
    patient_ledger,
    staff_charge,
    staff_charges,
)
from apps.billing.services import (
    BillingAccessDeniedError,
    BillingConflictError,
    BillingValueError,
    cancel_invoice,
    charge_for_operation,
    confirm_settlement,
    create_invoice,
    issue_invoice,
    release_invoice,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import patient_session_overview

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

logger = logging.getLogger(__name__)
POLL_SECONDS: Final = 4
POLL_LIMIT: Final = 15
CONFLICT: Final = 409
INVALID: Final = 400
UNAVAILABLE: Final = 503
DENIED_DETAIL: Final = "Não foi possível concluir a ação. A cobrança não foi alterada."
CONFLICT_DETAIL: Final = (
    "A cobrança mudou ou não está no estado esperado para esta ação. "
    "Nada foi alterado; confira o estado atual abaixo."
)
INVALID_DETAIL: Final = (
    "Confira a confirmação: o valor precisa ser igual ao da cobrança e a "
    "verificação precisa ser marcada. Nada foi registrado."
)
PIX_UNAVAILABLE_DETAIL: Final = (
    "A geração de código de pagamento não está disponível neste ambiente. "
    "Nenhuma cobrança foi criada."
)
CHARGE_CREATED_DETAIL: Final = "Cobrança criada como rascunho."
CHARGE_ALREADY_OPEN_DETAIL: Final = (
    "Esta cobrança já estava criada com estes mesmos dados. Nada foi "
    "duplicado; abaixo está a cobrança que já existe."
)
# One fixed namespace for charge-create operation names; see _operation_key.
CHARGE_OPERATION_NAMESPACE: Final = UUID("7577606c-0daf-44cb-8839-f010396ca1a5")


class InvalidSettlementError(Exception):
    """Carry the bound attestation form back to the unchanged charge screen."""

    def __init__(self, form: SettlementForm) -> None:
        """Keep the submitted values and their field errors for re-rendering."""
        super().__init__("settlement attestation invalid")
        self.form = form


def invoice_url(clinic_id: UUID, invoice_id: UUID) -> str:
    """Return the safe GET of one charge; unsafe methods resume here."""
    return reverse(
        "billing:invoice",
        kwargs={"clinic_id": clinic_id, "invoice_id": invoice_id},
    )


def charges_url(clinic_id: UUID) -> str:
    """Return the safe GET of the clinic charge list."""
    return reverse("billing:charges", kwargs={"clinic_id": clinic_id})


def _attempt(request: HttpRequest) -> int:
    raw = request.GET.get("attempt", "0")
    return min(int(raw), POLL_LIMIT) if raw.isdigit() else 0


def _poll_context(
    *, live: bool, attempt: int, poll_url: str, refresh_url: str
) -> dict[str, object]:
    """Bound the refresh loop in the server response, not in the browser."""
    return {
        "attempt": attempt,
        "next_attempt": attempt + 1,
        "poll": live and attempt < POLL_LIMIT,
        "poll_exhausted": live and attempt >= POLL_LIMIT,
        "poll_seconds": POLL_SECONDS,
        "poll_url": poll_url,
        "refresh_url": refresh_url,
    }


def _clinic_timezone(clinic_id: UUID) -> str:
    """Read the clinic zone every displayed time is rendered in."""
    return str(Clinic.objects.values_list("timezone", flat=True).get(pk=clinic_id))


def _enrolled(clinic_id: UUID) -> list[tuple[UUID, str]]:
    # Names are tenant envelopes: ordering happens after decryption.
    rows = PatientClinicEnrollment.objects.filter(clinic_id=clinic_id).values_list(
        "patient_id", "patient__full_name"
    )
    return sorted(
        ((patient_id, name) for patient_id, name in rows),
        key=lambda row: (row[1].lower(), str(row[0])),
    )


def _staff_context(clinic_id: UUID, **extra: object) -> dict[str, object]:
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "charges": staff_charges(clinic_id=clinic_id),
        "clinic_timezone": _clinic_timezone(clinic_id),
    }
    context.update(extra)
    return context


def _detail_context(
    request: HttpRequest, clinic_id: UUID, charge: StaffCharge, **extra: object
) -> dict[str, object]:
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "staff_charge": charge,
        "view": charge.charge,
        "detail": charge.detail,
        "clinic_timezone": _clinic_timezone(clinic_id),
        "settlement_form": SettlementForm(
            initial={"amount": charge.charge.amount},
        ),
        **_poll_context(
            live=charge.charge.live,
            attempt=_attempt(request),
            poll_url=reverse(
                "billing:invoice-status",
                kwargs={"clinic_id": clinic_id, "invoice_id": charge.charge.invoice_id},
            ),
            refresh_url=invoice_url(clinic_id, charge.charge.invoice_id),
        ),
    }
    context.update(extra)
    return context


def _charge_form(
    patients: list[tuple[UUID, str]], repeat: StaffCharge | None = None
) -> ChargeForm:
    """Offer the create form, prefilled when one charge is being repeated."""
    form = ChargeForm(patients=patients)
    if repeat is not None:
        form.initial.update(
            patient_id=str(repeat.patient_id),
            amount=repeat.charge.amount,
            repeat_of=str(repeat.charge.invoice_id),
        )
    return form


def _repeated_charge(request: HttpRequest, clinic_id: UUID) -> StaffCharge | None:
    """Read the charge this visit was explicitly asked to charge again."""
    asked = request.GET.get("repeat_of", "")
    if not asked:
        return None
    return staff_charge(clinic_id=clinic_id, invoice_id=UUID(asked))


def _operation_key(
    request: HttpRequest,
    clinic_id: UUID,
    patient_id: UUID,
    amount_minor: int,
    repeat_of: UUID | None,
) -> UUID:
    """Name this create operation by who is asking and for exactly what.

    The name is a pure function of the signed-in actor and session, the
    clinic, the patient, the BRL terms and the charge this submission
    deliberately repeats. Nothing is minted per rendered page, so a form the
    browser restored - Back, refresh, a resubmitted history entry - recomputes
    the same name and the database resolves it to the charge it already
    opened. Another session, another patient, another value or an explicit
    repeat is a different operation, so intentional charges stay distinct.
    """
    return uuid5(
        CHARGE_OPERATION_NAMESPACE,
        f"{request.user.pk}:{request.session.session_key}:{clinic_id}:"
        f"{patient_id}:{amount_minor}:{repeat_of or ''}",
    )


def _create(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Run one named create operation; its replay resolves to the same charge."""
    patients = _enrolled(clinic_id)
    form = ChargeForm(request.POST, patients=patients)
    if not form.is_valid():
        return render(
            request,
            "billing/charges.html",
            _staff_context(clinic_id, form=form, patients=patients),
            status=INVALID,
        )
    patient_id = UUID(str(form.cleaned_data["patient_id"]))
    amount_minor = int(form.cleaned_data["amount"])
    key = _operation_key(
        request, clinic_id, patient_id, amount_minor, form.cleaned_data["repeat_of"]
    )
    opened = charge_for_operation(clinic_id=clinic_id, idempotency_key=key)
    invoice = create_invoice(
        clinic_id=clinic_id,
        patient_id=patient_id,
        amount_minor=amount_minor,
        idempotency_key=key,
    )
    messages.success(
        request,
        CHARGE_ALREADY_OPEN_DETAIL if opened is not None else CHARGE_CREATED_DETAIL,
    )
    return redirect(invoice_url(clinic_id, invoice.pk))


@never_cache
@privileged_totp_required(charges_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def charges_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """List this clinic's charges and open new drafts for enrolled patients."""
    try:
        if request.method == "POST":
            if request.POST.get("action") != "create":
                return render(request, "403.html", status=403)
            return _create(request, clinic_id)
        repeat = _repeated_charge(request, clinic_id)
        return render(
            request,
            "billing/charges.html",
            _staff_context(
                clinic_id,
                form=_charge_form(_enrolled(clinic_id), repeat),
                repeat=repeat,
            ),
        )
    except (BillingAccessDeniedError, CurrentActorError, ValueError):
        return render(request, "403.html", status=403)
    except BillingValueError:
        return render(
            request,
            "billing/charges.html",
            _staff_context(
                clinic_id,
                form=_charge_form(_enrolled(clinic_id)),
                error="Informe um valor em reais com no máximo dois decimais.",
            ),
            status=INVALID,
        )


def _issue(request: HttpRequest, clinic_id: UUID, invoice_id: UUID) -> None:
    raw = request.POST.get("expected_revision", "")
    revision = int(raw) if raw.isdigit() else 0
    issue_invoice(
        clinic_id=clinic_id, invoice_id=invoice_id, expected_revision=revision
    )
    messages.success(request, "Cobrança emitida com valores congelados.")


def _code(request: HttpRequest, clinic_id: UUID, invoice_id: UUID) -> None:
    """Bind one stable request per charge; a retry converges on the same code.

    The request to reuse or replace is resolved from stored rows: a live
    request is completed again (returning its exact stored result) and only an
    expired one gets a linked successor, so refresh, back and double submit
    never produce a second live code.
    """
    head = current_operation(clinic_id=clinic_id, invoice_id=invoice_id)
    live = head is not None and head.expires_at > timezone.now()
    operation_id = (
        head.pk
        if live and head is not None
        else prepare_pix_charge(
            clinic_id=clinic_id,
            invoice_id=invoice_id,
            previous_id=head.pk if head is not None else None,
        ).pk
    )
    complete_pix_charge(
        clinic_id=clinic_id, invoice_id=invoice_id, operation_id=operation_id
    )
    messages.success(request, "Código de pagamento disponível.")


def _confirm(request: HttpRequest, clinic_id: UUID, invoice_id: UUID) -> None:
    form = SettlementForm(request.POST)
    if not form.is_valid():
        raise InvalidSettlementError(form)
    confirm_settlement(
        clinic_id=clinic_id,
        invoice_id=invoice_id,
        confirmation_reference=form.cleaned_data["confirmation_reference"],
        amount_minor=int(form.cleaned_data["amount"]),
    )
    messages.success(
        request, "Pagamento simulado registrado e recibo de ensaio emitido."
    )


def _act(request: HttpRequest, clinic_id: UUID, invoice_id: UUID) -> bool:
    """Run one explicit action; return False when the action is unknown."""
    action = request.POST.get("action")
    if action == "issue":
        _issue(request, clinic_id, invoice_id)
    elif action == "release":
        release_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
        messages.success(request, "Cobrança liberada para o paciente.")
    elif action in ("code", "regenerate"):
        _code(request, clinic_id, invoice_id)
    elif action == "confirm":
        _confirm(request, clinic_id, invoice_id)
    elif action == "cancel":
        cancel_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
        messages.success(request, "Cobrança cancelada.")
    else:
        return False
    return True


def _failed_detail(
    request: HttpRequest,
    clinic_id: UUID,
    invoice_id: UUID,
    *,
    error: str,
    status: int,
    **extra: object,
) -> HttpResponse:
    """Re-render the unchanged charge with the exact refusal reason."""
    charge = staff_charge(clinic_id=clinic_id, invoice_id=invoice_id)
    return render(
        request,
        "billing/invoice.html",
        _detail_context(request, clinic_id, charge, error=error, **extra),
        status=status,
    )


def _refusal(error: Exception) -> tuple[str, int]:
    """Map one refused action to its exact reason and status; nothing changed."""
    if isinstance(error, BillingConflictError):
        return (CONFLICT_DETAIL, CONFLICT)
    if isinstance(error, BillingValueError):
        return (INVALID_DETAIL, INVALID)
    if isinstance(error, PixUnavailableError):
        return (PIX_UNAVAILABLE_DETAIL, UNAVAILABLE)
    logger.log(logging.ERROR, "billing action failed; rolled back")
    return (DENIED_DETAIL, UNAVAILABLE)


def _invoice_response(
    request: HttpRequest, clinic_id: UUID, invoice_id: UUID
) -> HttpResponseBase:
    """Run one explicit action and redirect, or render the current charge."""
    if request.method == "POST":
        if not _act(request, clinic_id, invoice_id):
            return render(request, "403.html", status=403)
        return redirect(invoice_url(clinic_id, invoice_id))
    charge = staff_charge(clinic_id=clinic_id, invoice_id=invoice_id)
    return render(
        request, "billing/invoice.html", _detail_context(request, clinic_id, charge)
    )


@never_cache
@privileged_totp_required(invoice_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def invoice_detail(
    request: HttpRequest, clinic_id: UUID, invoice_id: UUID
) -> HttpResponseBase:
    """Show one charge, its payment code and the actions its state allows."""
    try:
        return _invoice_response(request, clinic_id, invoice_id)
    except InvalidSettlementError as refusal:
        return _failed_detail(
            request,
            clinic_id,
            invoice_id,
            error=INVALID_DETAIL,
            status=INVALID,
            settlement_form=refusal.form,
        )
    except (BillingAccessDeniedError, CurrentActorError, ValueError):
        return render(request, "403.html", status=403)
    except (
        BillingConflictError,
        BillingValueError,
        PixUnavailableError,
        DatabaseError,
    ) as error:
        message, status = _refusal(error)
        return _failed_detail(
            request, clinic_id, invoice_id, error=message, status=status
        )


@never_cache
@privileged_totp_required(invoice_url)
@require_http_methods(["GET"])
def invoice_status(
    request: HttpRequest, clinic_id: UUID, invoice_id: UUID
) -> HttpResponseBase:
    """Re-read one charge's stored state for the bounded refresh region."""
    try:
        charge = staff_charge(clinic_id=clinic_id, invoice_id=invoice_id)
    except (BillingAccessDeniedError, CurrentActorError, ValueError):
        return render(request, "403.html", status=403)
    return render(
        request,
        "billing/partials/staff_status.html",
        _detail_context(request, clinic_id, charge),
    )


def _patient_gate(request: HttpRequest) -> HttpResponse:
    return render(
        request, "intake/patient_gate.html", {"state": "required"}, status=403
    )


def _patient_allowed() -> bool:
    overview = patient_session_overview()
    return overview is not None and "billing" in overview.operations


@never_cache
@require_http_methods(["GET"])
def patient_charges_view(request: HttpRequest) -> HttpResponseBase:
    """List the released charges of the enrollment this session is bound to."""
    if not _patient_allowed():
        return _patient_gate(request)
    return render(
        request, "billing/patient_charges.html", {"charges": patient_ledger()}
    )


def _patient_context(request: HttpRequest, invoice_id: UUID) -> dict[str, object]:
    charge = patient_charge(invoice_id=invoice_id)
    if charge is None:
        return {}
    return {
        "patient_charge": charge,
        "view": charge.charge,
        "detail": charge.detail,
        "clinic_timezone": charge.clinic_timezone,
        **_poll_context(
            live=charge.charge.live,
            attempt=_attempt(request),
            poll_url=reverse(
                "billing:patient-charge-status", kwargs={"invoice_id": invoice_id}
            ),
            refresh_url=reverse(
                "billing:patient-charge", kwargs={"invoice_id": invoice_id}
            ),
        ),
    }


@never_cache
@require_http_methods(["GET"])
def patient_charge_view(request: HttpRequest, invoice_id: UUID) -> HttpResponseBase:
    """Show one own charge: value, payment instructions or the issued receipt."""
    if not _patient_allowed():
        return _patient_gate(request)
    context = _patient_context(request, invoice_id)
    if not context:
        return _patient_gate(request)
    return render(request, "billing/patient_charge.html", context)


@never_cache
@require_http_methods(["GET"])
def patient_charge_status(request: HttpRequest, invoice_id: UUID) -> HttpResponseBase:
    """Re-read one own charge for the patient's bounded refresh region."""
    if not _patient_allowed():
        return _patient_gate(request)
    context = _patient_context(request, invoice_id)
    if not context:
        return _patient_gate(request)
    return render(request, "billing/partials/patient_status.html", context)
