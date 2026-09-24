"""Native, no-store prescribing workflow: author, review, verify, sign, result.

Every screen keeps the same fixed context (patient, issuer, clinic and the
consultation) in view and re-derives it from stored records on each request;
nothing about the subject can change between the review and the signature.
Record selectors travel in POST bodies or in the operation/document path,
never in query strings. Action failures always re-render the full screen
with the entered content preserved and the true state named.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.http import HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_username,
    require_current_actor_clinic_roles,
)
from apps.identity.models import PhysicianProfile, UserClinicRole
from apps.identity.otp import flow_redirect, privileged_totp_required, safe_next_url
from apps.identity.physician_verification import PhysicianVerificationRequired
from apps.identity.stepup import (
    DEFAULT_STEP_UP_MAX_AGE_SECONDS,
    STEP_UP_SESSION_KEY,
    StepUpRequired,
    assert_step_up,
    set_step_up_intent,
)
from apps.intake.patient_access import patient_session_overview
from apps.prescription.document_rendering import DocumentRenderingError
from apps.prescription.forms import (
    PrescriptionDraftForm,
    PrescriptionItemForm,
    PrescriptionItemFormSet,
)
from apps.prescription.models import (
    COMPLETED_SIGNATURE_STATES,
    PrescriptionDocument,
    PrescriptionDocumentRelease,
    PrescriptionDocumentRevocation,
    PrescriptionDraft,
    SignatureOperation,
)
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import (
    DocumentStorageError,
    authorize_encounter,
    create_draft,
    discard_draft,
    download_document,
    draft_items,
    render_document,
    save_draft,
    view_draft,
)
from apps.prescription.signature_provider import (
    SYNTHETIC_PROVIDER,
    SignatureCallbackError,
)
from apps.prescription.signing import (
    SignatureUnavailableError,
    abandon_signature,
    download_signed_document,
    receive_signature_callback,
    request_signature,
    retry_dispatch,
    signature_status,
)
from apps.prescription.verification import (
    VerificationLimitedError,
    VerificationUnavailableError,
    deliver_document,
    patient_document_download,
    patient_documents,
    release_document,
    revoke_document,
    revoke_document_release,
    verify_handle,
)

if TYPE_CHECKING:
    from django.forms import BaseFormSet
    from django.http import HttpRequest

    from apps.ehr.models import Encounter

logger = logging.getLogger(__name__)
OK = 200
CONFLICT = 409
_SIGN_LOCK_NAMESPACE: Final = "clinic-lock-v1:prescription-sign:"
# Operations already handed to the identity/provider boundary. A ``draft``
# operation left behind by an interrupted step-up or evidence check is not
# in flight; the service supersedes it on the next authorized attempt.
_IN_FLIGHT_STATES: Final = (
    SignatureOperation.State.PREPARED,
    SignatureOperation.State.SIGNING,
)
_ITEM_FIELDS: Final = (
    ("medication_description", "Descrição do medicamento"),
    ("strength_form", "Concentração e forma"),
    ("dose", "Dose"),
    ("route", "Via"),
    ("frequency", "Frequência"),
    ("duration", "Duração"),
    ("quantity", "Quantidade"),
    ("instructions", "Orientações registradas pelo médico"),
)
# Failure reasons the signing lifecycle records, in the physician's words,
# each with the recovery it allows. Unknown reasons fall back to a generic
# line rather than to silence.
_FAILURE_TEXT: Final[dict[str, tuple[str, str]]] = {
    "provider_rejected": (
        "O provedor recusou a operação.",
        "O documento não foi alterado. Você pode tentar novamente.",
    ),
    "provider_unavailable": (
        "O provedor de assinatura não estava disponível.",
        "Nada foi emitido. Tente novamente mais tarde com o mesmo documento.",
    ),
    "invalid_provider_reference": (
        "O provedor devolveu uma referência inválida.",
        "Nada foi emitido. Tente novamente com o mesmo documento.",
    ),
    "content_digest_mismatch": (
        "O conteúdo armazenado não confere com o resumo registrado.",
        "Não use este artefato. Renderize uma nova versão a partir do rascunho.",
    ),
    "provider_reported": (
        "O provedor informou que a assinatura falhou.",
        "Nada foi emitido. Você pode tentar novamente com o mesmo documento.",
    ),
    "authorization_expired": (
        "A autorização expirou antes de a assinatura ser concluída.",
        "Confirme sua identidade novamente e assine de novo o mesmo documento.",
    ),
    "signature_invalid": (
        "A assinatura devolvida não passou na verificação independente.",
        "Nada foi emitido. Você pode tentar novamente com o mesmo documento.",
    ),
    "abandoned": (
        "Operação abandonada por você.",
        "O documento continua disponível para uma nova assinatura.",
    ),
    "superseded": (
        "Substituída por uma nova tentativa de assinatura.",
        "Acompanhe a tentativa mais recente.",
    ),
}
_GENERIC_FAILURE: Final = (
    "A assinatura falhou.",
    "Nada foi emitido. Você pode tentar novamente com o mesmo documento.",
)
_VERIFICATION_TEXT: Final[dict[str, str]] = {
    "clinical_authority_required": (
        "Você não está autorizado a assinar neste atendimento."
    ),
    "authenticated_issuer_mismatch": "A sessão atual não corresponde ao emissor.",
    "assigned_physician_required": "Somente o médico responsável pode assinar.",
    "registration_profile_required": (
        "Seu registro profissional não está provisionado para esta clínica."
    ),
    "synthetic_identity_required": (
        "Somente a identidade sintética de ensaio está habilitada."
    ),
    "signing_identity_mismatch": (
        "A identidade de assinatura não corresponde ao perfil."
    ),
    "registry_unavailable": "O registro profissional não respondeu.",
    "registry_response_invalid": "O registro profissional devolveu dados inválidos.",
    "registration_evidence_stale": "A evidência do registro profissional está vencida.",
    "registration_expired": "Seu registro profissional consta como expirado.",
}


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What an action left behind when it did not navigate away.

    ``code`` is the stable machine-consumed reason the screen renders as
    ``data-outcome``; ``error`` is the sentence a physician reads. The code
    never carries caller content.
    """

    status: int
    error: str
    code: str


def continuation(clinic_id: UUID, encounter_id: UUID | None = None) -> str:
    """Use a non-clinical continuation URL through TOTP.

    When the workspace is bound to one encounter the continuation keeps that
    binding, so returning navigation never resolves a different encounter.
    """
    if encounter_id is not None:
        return reverse(
            "prescription:draft-encounter",
            kwargs={"clinic_id": clinic_id, "encounter_id": encounter_id},
        )
    return reverse("prescription:draft", kwargs={"clinic_id": clinic_id})


def review_continuation(clinic_id: UUID, document_id: UUID) -> str:
    """Resume the review of one exact document after the identity step."""
    return reverse(
        "prescription:review",
        kwargs={"clinic_id": clinic_id, "document_id": document_id},
    )


def signing_continuation(clinic_id: UUID, operation_id: UUID) -> str:
    """Resume the operation status page after TOTP."""
    return reverse(
        "prescription:signing",
        kwargs={"clinic_id": clinic_id, "operation_id": operation_id},
    )


# ---------------------------------------------------------------------------
# Fixed context shared by every screen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    """The fixed subject every screen shows; derived from stored records only."""

    patient_name: str
    patient_birth: date
    issuer_name: str
    issuer_registration: str
    issuer_synthetic: bool
    clinic_name: str
    clinic_timezone: str
    encounter_at: datetime
    encounter_id: UUID


def _subject(encounter: Encounter) -> Subject:
    clinic = encounter.clinic
    profile = PhysicianProfile.objects.filter(
        organization_id=encounter.organization_id,
        user_id=encounter.physician_id,
        jurisdiction=clinic.crm_uf,
    ).first()
    return Subject(
        patient_name=encounter.patient.full_name,
        patient_birth=encounter.patient.birth_date,
        issuer_name=current_actor_username(),
        issuer_registration=(
            f"{profile.jurisdiction} {profile.registration_number}"
            if profile is not None
            else ""
        ),
        issuer_synthetic=profile.synthetic if profile is not None else True,
        clinic_name=clinic.name,
        clinic_timezone=str(clinic.timezone),
        encounter_at=encounter.appointment.start_at,
        encounter_id=encounter.pk,
    )


def _screen(
    clinic_id: UUID, encounter: Encounter, subject: Subject
) -> dict[str, object]:
    """Context shared by every screen of the workflow."""
    return {"clinic_id": clinic_id, "encounter": encounter, "subject": subject}


def _identity_state(request: HttpRequest) -> dict[str, object]:
    """Report whether the session's step-up is fresh, without consuming it."""
    fresh = True
    try:
        assert_step_up(request)
    except StepUpRequired:
        fresh = False
    verified_at = request.session.get(STEP_UP_SESSION_KEY)
    valid_until = (
        datetime.fromtimestamp(verified_at, UTC)
        + timedelta(seconds=DEFAULT_STEP_UP_MAX_AGE_SECONDS)
        if fresh and isinstance(verified_at, int) and not isinstance(verified_at, bool)
        else None
    )
    return {"fresh": fresh, "valid_until": valid_until}


def _step_up_intent(
    request: HttpRequest,
    target: str,
    document: PrescriptionDocument,
    subject: Subject,
) -> None:
    set_step_up_intent(
        request,
        target=target,
        action=f"Assinar o documento versão {document.document_version}",
        facts=[
            ("Paciente", subject.patient_name),
            ("Médico", subject.issuer_name),
            ("Clínica", subject.clinic_name),
            ("Resumo do conteúdo", f"SHA-256 {document.pdf_digest[:16]}…"),
        ],
    )


# ---------------------------------------------------------------------------
# Presentation helpers for documents and operations
# ---------------------------------------------------------------------------


def _operation_view(operation: SignatureOperation) -> dict[str, object]:
    """Describe one operation's true state for the screen."""
    failed = operation.state == SignatureOperation.State.FAILED
    title, recovery = _FAILURE_TEXT.get(operation.failure_reason, _GENERIC_FAILURE)
    synthetic = operation.provider == SYNTHETIC_PROVIDER
    return {
        "operation": operation,
        "live": operation.state in _IN_FLIGHT_STATES,
        "interrupted": operation.state == SignatureOperation.State.DRAFT,
        "complete": operation.state in COMPLETED_SIGNATURE_STATES,
        "failed": failed,
        "synthetic": synthetic,
        "provider_label": (
            "Provedor sintético de ensaio" if synthetic else operation.provider
        ),
        "failure_title": title if failed else "",
        "failure_recovery": recovery if failed else "",
        "badge": _badge_for(operation),
    }


_BADGES: Final[dict[str, tuple[str, str]]] = {
    SignatureOperation.State.REHEARSAL_COMPLETE: (
        "success",
        "Ensaio concluído · sintético",
    ),
    SignatureOperation.State.ISSUED: ("success", "Emitido"),
    SignatureOperation.State.FAILED: ("error", "Assinatura falhou"),
    SignatureOperation.State.SIGNING: ("pending", "Aguardando o provedor"),
    SignatureOperation.State.PREPARED: ("pending", "Preparado, aguardando envio"),
    SignatureOperation.State.DRAFT: ("pending", "Interrompida antes da confirmação"),
}


def _badge_for(operation: SignatureOperation | None) -> tuple[str, str]:
    """Map a state to (tone, label); labels always carry a word."""
    if operation is None:
        return ("muted", "Sem assinatura")
    return _BADGES.get(operation.state, ("pending", "Em andamento"))


def _progress(operation: SignatureOperation) -> list[dict[str, str]]:
    """Render the operation as explicit steps: done, current, failed, pending."""
    prepared = operation.evidence_id is not None
    dispatched = operation.operation_id != ""
    complete = operation.state in COMPLETED_SIGNATURE_STATES
    failed = operation.state == SignatureOperation.State.FAILED
    labels = (
        ("prepared", "Identidade e registro verificados"),
        ("dispatched", "Enviado ao provedor"),
        ("verified", "Retorno do provedor verificado"),
    )
    done = {
        "prepared": prepared,
        "dispatched": dispatched,
        "verified": complete,
    }
    steps: list[dict[str, str]] = []
    marked = False
    for key, label in labels:
        if done[key]:
            state = "done"
        elif failed and not marked:
            state, marked = "failed", True
        elif not marked and not failed:
            state, marked = "current", True
        else:
            state = "pending"
        steps.append({"key": key, "label": label, "state": state})
    return steps


def _document_rows(
    draft: PrescriptionDraft | None, clinic_id: UUID
) -> list[dict[str, object]]:
    """Build the immutable rendered-document history for the author screen."""
    if draft is None:
        return []
    documents = list(
        PrescriptionDocument.objects.filter(draft=draft).order_by("document_version")
    )
    operations: dict[UUID, list[SignatureOperation]] = {}
    for operation in SignatureOperation.objects.filter(document__in=documents).order_by(
        "created_at"
    ):
        operations.setdefault(operation.document_id, []).append(operation)
    releases = {
        release.document_id: release
        for release in PrescriptionDocumentRelease.objects.filter(
            document__in=documents, revoked_at__isnull=True
        )
    }
    revoked_ids = set(
        PrescriptionDocumentRevocation.objects.filter(
            document__in=documents
        ).values_list("document_id", flat=True)
    )
    completed_versions = [
        document.document_version
        for document in documents
        if any(
            operation.state in COMPLETED_SIGNATURE_STATES
            for operation in operations.get(document.pk, [])
        )
    ]
    rows: list[dict[str, object]] = []
    for document in documents:
        attempts = operations.get(document.pk, [])
        latest = attempts[-1] if attempts else None
        complete = latest is not None and latest.state in COMPLETED_SIGNATURE_STATES
        live = latest is not None and latest.state in _IN_FLIGHT_STATES
        revoked = document.pk in revoked_ids
        superseded = complete and any(
            version > document.document_version for version in completed_versions
        )
        tone, label = _badge_for(latest)
        if revoked:
            tone, label = ("muted", "Revogado")
        elif superseded:
            tone, label = ("muted", "Substituído pela versão mais nova")
        rows.append(
            {
                "document": document,
                "operation": latest,
                "attempts": len(attempts),
                "release": releases.get(document.pk),
                "revoked": revoked,
                "superseded": superseded,
                "complete": complete,
                "live": live,
                "can_review": not complete and not live,
                "badge_tone": tone,
                "badge_label": label,
                "review_url": review_continuation(clinic_id, document.pk),
                "signing_url": (
                    signing_continuation(clinic_id, latest.pk)
                    if latest is not None
                    else ""
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _save_bound_form(
    request: HttpRequest,
    clinic_id: UUID,
    form: PrescriptionDraftForm,
    items: BaseFormSet[PrescriptionItemForm],
) -> tuple[int, str]:
    action = request.POST.get("action")
    if action not in {"save", "discard"}:
        raise ClinicalAccessDeniedError
    if not form.is_valid() or (action == "save" and not items.is_valid()):
        return 400, ""
    values = dict(form.cleaned_data)
    try:
        if action == "discard":
            values.pop("category")
            discard_draft(clinic_id=clinic_id, **values)
        else:
            save_draft(
                clinic_id=clinic_id,
                items=[item for item in items.cleaned_data if item],
                **values,
            )
    except ClinicalConflictError:
        return CONFLICT, (
            "Outra versão foi salva. Suas alterações não foram guardadas. "
            "Reabra a versão salva."
        )
    except ValidationError as exc:
        form.add_error(None, exc)
        return 400, ""
    except DatabaseError:
        logger.log(logging.ERROR, "prescription save failed; transaction rolled back")
        return 503, "Não foi possível salvar. Suas alterações não foram guardadas."
    return OK, ""


def _attachment(data: bytes, content_type: str, file_name: str) -> HttpResponse:
    """Return a bounded, fully materialized, never-cached artifact response."""
    response = HttpResponse(data, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{file_name}"'
    response["Content-Length"] = str(len(data))
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


def _download_document(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    result = download_document(
        clinic_id=clinic_id, document_id=UUID(request.POST.get("document_id", ""))
    )
    return _attachment(result.data, result.content_type, result.file_name)


def _render_document(
    request: HttpRequest,
    clinic_id: UUID,
    encounter: Encounter,
    draft: PrescriptionDraft | None,
) -> HttpResponseBase | ActionOutcome:
    """Freeze the current draft version, then open its review.

    A repeated submit for an already rendered version reopens that exact
    document instead of failing: the artifact is immutable, so the second
    click has nothing new to produce.
    """
    if draft is None or draft.state != "draft":
        raise ClinicalAccessDeniedError
    expected = int(request.POST.get("expected_version", "0"))
    try:
        document = render_document(
            clinic_id=clinic_id,
            draft_id=draft.pk,
            encounter_id=encounter.pk,
            patient_id=encounter.patient_id,
            issuer_id=encounter.physician_id,
            expected_version=expected,
        )
    except DocumentRenderingError:
        logger.log(logging.ERROR, "prescription document render failed")
        return ActionOutcome(
            503,
            "A renderização não está disponível no momento. Nada foi criado.",
            "render_unavailable",
        )
    except ClinicalConflictError as conflict:
        if conflict.reason_code == "already_rendered":
            existing = PrescriptionDocument.objects.filter(
                draft=draft, document_version=expected
            ).first()
            if existing is not None:
                return redirect(review_continuation(clinic_id, existing.pk))
        return ActionOutcome(
            CONFLICT,
            "O rascunho mudou desde que esta página foi aberta. "
            "Reabra a versão salva e revise antes de renderizar.",
            "draft_moved",
        )
    return redirect(review_continuation(clinic_id, document.pk))


def _hold_signing_lock(document_id: UUID) -> None:
    """Serialize sign requests per document for the rest of the transaction.

    Two submits of the same button (a double click, a retried request) queue
    on this lock; the second one finds the operation the first created and
    resumes it instead of starting another. Disabled buttons are feedback;
    this lock is the guarantee.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [f"{_SIGN_LOCK_NAMESPACE}{document_id}"],
        )


def _sign_document(
    request: HttpRequest,
    clinic_id: UUID,
    document_id: UUID,
    *,
    step_up_target: str,
    subject: Subject,
) -> HttpResponseBase | ActionOutcome:
    """Start signing once per document, or resume the in-flight operation."""
    _hold_signing_lock(document_id)
    existing = SignatureOperation.objects.filter(
        document_id=document_id,
        state__in=_IN_FLIGHT_STATES,
    ).first()
    if existing is not None:
        return redirect(signing_continuation(clinic_id, existing.pk))
    try:
        operation = request_signature(
            request=request, clinic_id=clinic_id, document_id=document_id
        )
    except StepUpRequired:
        document = PrescriptionDocument.objects.filter(
            pk=document_id, clinic_id=clinic_id
        ).first()
        if document is not None:
            _step_up_intent(request, step_up_target, document, subject)
        return flow_redirect(
            request, "identity:step-up", safe_next_url(request, step_up_target)
        )
    except PhysicianVerificationRequired as denied:
        return ActionOutcome(
            403,
            "A verificação profissional falhou. "
            + _VERIFICATION_TEXT.get(
                denied.reason_code, "A verificação profissional não está disponível."
            ),
            denied.reason_code,
        )
    except SignatureUnavailableError:
        return ActionOutcome(
            503,
            "A assinatura não está disponível no momento. Nada foi emitido.",
            "signature_unavailable",
        )
    except ClinicalConflictError as conflict:
        return ActionOutcome(
            CONFLICT,
            "Este documento já foi assinado ou está em assinatura.",
            conflict.reason_code,
        )
    return redirect(signing_continuation(clinic_id, operation.pk))


def _document_action(
    request: HttpRequest,
    clinic_id: UUID,
    encounter: Encounter,
    draft: PrescriptionDraft | None,
    subject: Subject,
) -> HttpResponseBase | ActionOutcome | None:
    """Dispatch document actions; ``None`` means continue the workspace flow."""
    action = request.POST.get("action")
    if action == "create":
        # Even creation assertions are checked; they are never used as authority.
        create_draft(
            clinic_id=clinic_id,
            encounter_id=encounter.pk,
            patient_id=UUID(request.POST.get("patient_id", "")),
            issuer_id=UUID(request.POST.get("issuer_id", "")),
            category=request.POST.get("category", ""),
        )
        return redirect(continuation(clinic_id, encounter.pk))
    if action == "download_document":
        return _download_document(request, clinic_id)
    if action == "render_document":
        return _render_document(request, clinic_id, encounter, draft)
    if action in {"review_document", "sign_document"}:
        return _open_or_sign(request, clinic_id, draft, action, subject)
    if action in {
        "release_document",
        "revoke_release",
        "revoke_document",
        "deliver_document",
    }:
        return _document_lifecycle_action(request, clinic_id, encounter, action)
    return None


def _open_or_sign(
    request: HttpRequest,
    clinic_id: UUID,
    draft: PrescriptionDraft | None,
    action: str,
    subject: Subject,
) -> HttpResponseBase | ActionOutcome:
    """Open the review of one rendered document, or start signing it."""
    document = _draft_document(draft, UUID(request.POST.get("document_id", "")))
    if action == "review_document":
        return redirect(review_continuation(clinic_id, document.pk))
    if draft is None or draft.state != "draft":
        raise ClinicalAccessDeniedError
    return _sign_document(
        request,
        clinic_id,
        document.pk,
        step_up_target=review_continuation(clinic_id, document.pk),
        subject=subject,
    )


def _draft_document(
    draft: PrescriptionDraft | None, document_id: UUID
) -> PrescriptionDocument:
    """Resolve a document only within the workspace's own draft."""
    if draft is None:
        raise ClinicalAccessDeniedError
    document = PrescriptionDocument.objects.filter(pk=document_id, draft=draft).first()
    if document is None:
        raise ClinicalAccessDeniedError
    return document


def _document_lifecycle_action(
    request: HttpRequest, clinic_id: UUID, encounter: Encounter, action: str
) -> HttpResponseBase | ActionOutcome:
    """Run one issuer-bound release, revocation or delivery action.

    Release, revocation and delivery are idempotent in the service; a
    revocation of an already revoked release simply shows the current state.
    Conflicts name the real precondition instead of a generic refusal.
    """
    document_id = UUID(request.POST.get("document_id", ""))
    try:
        if action == "release_document":
            release_document(clinic_id=clinic_id, document_id=document_id)
        elif action == "revoke_release":
            if PrescriptionDocumentRelease.objects.filter(
                document_id=document_id, revoked_at__isnull=True
            ).exists():
                revoke_document_release(clinic_id=clinic_id, document_id=document_id)
        elif action == "deliver_document":
            deliver_document(clinic_id=clinic_id, document_id=document_id)
        else:
            reason = request.POST.get("reason", "")
            revoke_document(clinic_id=clinic_id, document_id=document_id, reason=reason)
    except ClinicalConflictError as conflict:
        messages = {
            "not_issued": "Este documento ainda não tem assinatura concluída.",
            "revoked": "Este documento foi revogado; nenhum aviso pode ser enviado.",
            "not_released": "Libere o documento ao paciente antes de enviar o aviso.",
            "no_verified_contact": (
                "O paciente não tem e-mail verificado; nenhum aviso foi enviado."
            ),
        }
        return ActionOutcome(
            CONFLICT,
            messages.get(conflict.reason_code, "A ação não pôde ser concluída."),
            conflict.reason_code,
        )
    return redirect(continuation(clinic_id, encounter.pk))


# ---------------------------------------------------------------------------
# Author screen
# ---------------------------------------------------------------------------


def _selected_encounter(
    request: HttpRequest, clinic_id: UUID, key: str, encounter_id: UUID | None
) -> Encounter | None:
    """Resolve the bound path, or the legacy POST/session selector, or deny."""
    if encounter_id is not None:
        posted = request.POST.get("encounter_id") if request.method == "POST" else None
        if posted and UUID(str(posted)) != encounter_id:
            raise ClinicalAccessDeniedError
        encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
        request.session[key] = str(encounter.pk)
        return encounter
    selected = (
        request.POST.get("encounter_id")
        if request.method == "POST"
        else request.session.get(key)
    )
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return None
    return authorize_encounter(clinic_id=clinic_id, encounter_id=UUID(str(selected)))


def _draft_forms(
    request: HttpRequest,
    clinic_id: UUID,
    draft: PrescriptionDraft,
    scope: dict[str, UUID],
    *,
    bound: bool,
) -> tuple[PrescriptionDraftForm, BaseFormSet[PrescriptionItemForm], int, str]:
    """Build the author forms; a bound POST is the explicit save or discard."""
    initial = {
        **scope,
        "draft_id": draft.pk,
        "expected_version": draft.version,
        "category": draft.category,
    }
    form = PrescriptionDraftForm(request.POST if bound else None, initial=initial)
    items = PrescriptionItemFormSet(
        request.POST if bound else None,
        initial=draft_items(draft),
        prefix="items",
    )
    status, error = (
        _save_bound_form(request, clinic_id, form, items) if bound else (OK, "")
    )
    return form, items, status, error


def _workspace(
    request: HttpRequest, clinic_id: UUID, encounter_id: UUID | None = None
) -> HttpResponseBase:
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    key = f"prescription.encounter.{clinic_id}"
    encounter = _selected_encounter(request, clinic_id, key, encounter_id)
    if encounter is None:
        return render(request, "prescription/draft.html", {"clinic_id": clinic_id})
    scope = {
        "clinic_id": clinic_id,
        "encounter_id": encounter.pk,
        "patient_id": encounter.patient_id,
        "issuer_id": encounter.physician_id,
    }
    if request.method == "POST" and request.POST.get("action") == "open":
        request.session[key] = str(encounter.pk)
        return redirect(continuation(clinic_id, encounter.pk))
    subject = _subject(encounter)
    draft = PrescriptionDraft.objects.filter(encounter=encounter).first()
    outcome: ActionOutcome | None = None
    if request.method == "POST":
        result = _document_action(request, clinic_id, encounter, draft, subject)
        if isinstance(result, ActionOutcome):
            outcome = result
        elif result is not None:
            return result
    # A POST that no document action consumed is the explicit save/discard.
    bound = request.method == "POST" and outcome is None
    form = items = None
    status, error = (outcome.status, outcome.error) if outcome else (OK, "")
    code = outcome.code if outcome else ""
    if draft is not None and draft.state == "draft":
        draft = view_draft(draft_id=draft.pk, **scope)
        form, items, saved, save_error = _draft_forms(
            request, clinic_id, draft, scope, bound=bound
        )
        if bound and saved == OK:
            return redirect(continuation(clinic_id, encounter.pk))
        if bound:
            status, error = saved, save_error
    elif bound:
        raise ClinicalAccessDeniedError
    return render(
        request,
        "prescription/draft.html",
        {
            **_screen(clinic_id, encounter, subject),
            "draft": draft,
            "form": form,
            "items": items,
            "documents": _document_rows(draft, clinic_id),
            "category": SYNTHETIC_CATEGORY,
            "error": error,
            "error_code": code,
            "unsaved": bound and status != OK,
            "step": "author",
        },
        status=status,
    )


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def draft_workspace(
    request: HttpRequest, clinic_id: UUID, encounter_id: UUID | None = None
) -> HttpResponseBase:
    """Keep failures non-enumerating and unsaved medication text in its bound form."""
    try:
        return _workspace(request, clinic_id, encounter_id)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except DocumentStorageError:
        logger.log(logging.ERROR, "prescription document storage failed")
        return render(request, "403.html", status=403)
    except (ValidationError, ClinicalConflictError):
        return render(
            request,
            "prescription/draft.html",
            {
                "clinic_id": clinic_id,
                "error": (
                    "Rascunho não criado: categoria indisponível "
                    "ou atendimento encerrado."
                ),
            },
            status=400,
        )


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


def _issuer_document(clinic_id: UUID, document_id: UUID) -> PrescriptionDocument:
    """Resolve one document for its assigned issuer under current assignment."""
    document = (
        PrescriptionDocument.objects.filter(pk=document_id, clinic_id=clinic_id)
        .select_related("draft", "patient", "clinic")
        .first()
    )
    if document is None:
        raise ClinicalAccessDeniedError
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=document.encounter_id
    )
    if document.issuer_id != encounter.physician_id:
        raise ClinicalAccessDeniedError
    return document


def _review_items(document: PrescriptionDocument) -> list[list[dict[str, str]]]:
    """Present the exact frozen items, field by field, with their labels."""
    frozen = document.frozen_input
    raw_items = frozen.get("items") if isinstance(frozen, dict) else None
    items: list[list[dict[str, str]]] = []
    if not isinstance(raw_items, list):
        return items
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        items.append(
            [
                {"key": key, "label": label, "value": str(raw.get(key, ""))}
                for key, label in _ITEM_FIELDS
            ]
        )
    return items


def _review(
    request: HttpRequest,
    clinic_id: UUID,
    document: PrescriptionDocument,
    outcome: ActionOutcome | None,
) -> HttpResponseBase:
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=document.encounter_id
    )
    subject = _subject(encounter)
    frozen = document.frozen_input if isinstance(document.frozen_input, dict) else {}
    attempts = [
        _operation_view(operation)
        for operation in SignatureOperation.objects.filter(document=document).order_by(
            "created_at"
        )
    ]
    live = next((view for view in attempts if view["live"]), None)
    complete = next((view for view in attempts if view["complete"]), None)
    revoked = PrescriptionDocumentRevocation.objects.filter(document=document).exists()
    draft = document.draft
    drift = [
        label
        for label, frozen_value, current in (
            ("paciente", frozen.get("patient_label"), subject.patient_name),
            ("clínica", frozen.get("clinic_label"), subject.clinic_name),
            ("médico", frozen.get("issuer_label"), subject.issuer_name),
        )
        if frozen_value != current
    ]
    verify_path = reverse(
        "prescription:verify-document", kwargs={"handle": document.qr_handle}
    )
    return render(
        request,
        "prescription/review.html",
        {
            **_screen(clinic_id, encounter, subject),
            "document": document,
            "draft": draft,
            "frozen": frozen,
            "items": _review_items(document),
            "attempts": attempts,
            "live": live,
            "complete": complete,
            "last_failed": next(
                (view for view in reversed(attempts) if view["failed"]), None
            ),
            "revoked": revoked,
            "stale": (
                draft.state == "draft" and draft.version != document.document_version
            ),
            "drift": drift,
            "identity": _identity_state(request),
            "verify_path": verify_path,
            "error": outcome.error if outcome else "",
            "error_code": outcome.code if outcome else "",
            "step": "review",
        },
        status=outcome.status if outcome else 200,
    )


def _review_action(
    request: HttpRequest,
    clinic_id: UUID,
    document: PrescriptionDocument,
) -> HttpResponseBase | ActionOutcome:
    action = request.POST.get("action")
    if action == "download_document":
        result = download_document(clinic_id=clinic_id, document_id=document.pk)
        return _attachment(result.data, result.content_type, result.file_name)
    if action == "sign_document":
        if document.draft.state != "draft":
            raise ClinicalAccessDeniedError
        encounter = authorize_encounter(
            clinic_id=clinic_id, encounter_id=document.encounter_id
        )
        return _sign_document(
            request,
            clinic_id,
            document.pk,
            step_up_target=review_continuation(clinic_id, document.pk),
            subject=_subject(encounter),
        )
    raise ClinicalAccessDeniedError


@never_cache
@privileged_totp_required(review_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def review_view(
    request: HttpRequest, clinic_id: UUID, document_id: UUID
) -> HttpResponseBase:
    """Show the exact rendered content and start signing from it.

    This is the step-up continuation: after the identity challenge the
    physician lands back here, on the same document, with the identity
    state named, and confirms the signature explicitly once more.
    """
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
        document = _issuer_document(clinic_id, document_id)
        outcome: ActionOutcome | None = None
        if request.method == "POST":
            result = _review_action(request, clinic_id, document)
            if isinstance(result, ActionOutcome):
                outcome = result
            else:
                return result
        return _review(request, clinic_id, document, outcome)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except DocumentStorageError:
        logger.log(logging.ERROR, "prescription document storage failed")
        return render(request, "403.html", status=403)


# ---------------------------------------------------------------------------
# Signature and result screen
# ---------------------------------------------------------------------------


def _restart_signature(
    request: HttpRequest,
    clinic_id: UUID,
    operation: SignatureOperation,
) -> HttpResponseBase | ActionOutcome:
    """Start an authorized fresh attempt on the same immutable document.

    Failed or abandoned operations are terminal, so recovery re-runs the
    full boundary: fresh step-up, a current registry evidence check and a
    new operation bound to the unchanged document bytes.
    """
    if operation.state != SignatureOperation.State.FAILED:
        raise ClinicalAccessDeniedError
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=operation.encounter_id
    )
    return _sign_document(
        request,
        clinic_id,
        operation.document_id,
        step_up_target=signing_continuation(clinic_id, operation.pk),
        subject=_subject(encounter),
    )


def _signing_action(
    request: HttpRequest,
    clinic_id: UUID,
    operation: SignatureOperation,
) -> HttpResponseBase | ActionOutcome:
    """Dispatch status-page actions."""
    action = request.POST.get("action")
    if action == "restart_signature":
        return _restart_signature(request, clinic_id, operation)
    if action == "download_signed":
        result = download_signed_document(
            clinic_id=clinic_id, operation_id=operation.pk
        )
        return _attachment(result.data, result.content_type, result.file_name)
    if action == "retry_dispatch":
        try:
            retry_dispatch(clinic_id=clinic_id, operation_id=operation.pk)
        except ClinicalConflictError as conflict:
            return ActionOutcome(
                CONFLICT,
                "A operação já saiu do estado preparado; veja o estado atual.",
                conflict.reason_code,
            )
        return redirect(signing_continuation(clinic_id, operation.pk))
    if action == "abandon":
        try:
            abandon_signature(clinic_id=clinic_id, operation_id=operation.pk)
        except ClinicalConflictError as conflict:
            return ActionOutcome(
                CONFLICT,
                "A operação já terminou; veja o estado atual.",
                conflict.reason_code,
            )
        return redirect(signing_continuation(clinic_id, operation.pk))
    raise ClinicalAccessDeniedError


def _evidence_facts(operation: SignatureOperation) -> dict[str, object]:
    """Present the frozen evidence snapshot; timestamps become clinic-local."""
    snapshot = operation.evidence_snapshot
    if not isinstance(snapshot, dict):
        return {}
    facts: dict[str, object] = dict(snapshot)
    for key in ("checked_at", "expires_at", "recheck_at"):
        raw = snapshot.get(key)
        if isinstance(raw, str):
            try:
                facts[key] = datetime.fromisoformat(raw)
            except ValueError:
                facts[key] = raw
    return facts


def _signing_page(
    request: HttpRequest,
    clinic_id: UUID,
    operation: SignatureOperation,
    outcome: ActionOutcome | None,
) -> HttpResponseBase:
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=operation.encounter_id
    )
    document = operation.document
    view = _operation_view(operation)
    siblings = list(
        SignatureOperation.objects.filter(document=document).order_by("created_at")
    )
    attempts = [_operation_view(sibling) for sibling in siblings]
    # A reopened page for an old attempt must point at the newest one, not
    # offer a restart: the document's signing state lives on the latest
    # attempt, which may be in flight or already completed.
    newer_attempt = attempts[-1] if siblings[-1].pk != operation.pk else None
    revoked = PrescriptionDocumentRevocation.objects.filter(document=document).exists()
    release = PrescriptionDocumentRelease.objects.filter(
        document=document, revoked_at__isnull=True
    ).first()
    return render(
        request,
        "prescription/signing.html",
        {
            **_screen(clinic_id, encounter, _subject(encounter)),
            "operation": operation,
            "document": document,
            "view": view,
            "progress": _progress(operation),
            "attempts": attempts,
            "newer_attempt": newer_attempt,
            "evidence": _evidence_facts(operation),
            "revoked": revoked,
            "release": release,
            "verify_path": reverse(
                "prescription:verify-document", kwargs={"handle": document.qr_handle}
            ),
            "review_url": review_continuation(clinic_id, document.pk),
            "error": outcome.error if outcome else "",
            "error_code": outcome.code if outcome else "",
            "step": "result" if view["complete"] or view["failed"] else "sign",
        },
        status=outcome.status if outcome else 200,
    )


@never_cache
@privileged_totp_required(signing_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def signing_status_view(
    request: HttpRequest, clinic_id: UUID, operation_id: UUID
) -> HttpResponseBase:
    """Show the explicit lifecycle state and allow bounded recovery actions."""
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
        operation = signature_status(clinic_id=clinic_id, operation_id=operation_id)
        outcome: ActionOutcome | None = None
        if request.method == "POST":
            result = _signing_action(request, clinic_id, operation)
            if isinstance(result, ActionOutcome):
                outcome = result
            else:
                return result
        return _signing_page(request, clinic_id, operation, outcome)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except DocumentStorageError:
        logger.log(logging.ERROR, "prescription signed document storage failed")
        return render(request, "403.html", status=403)
    except ClinicalConflictError:
        return render(request, "403.html", status=403)


# ---------------------------------------------------------------------------
# Public verification, patient documents and provider callback
# ---------------------------------------------------------------------------


@never_cache
@require_http_methods(["GET"])
def verify_document_view(request: HttpRequest, handle: str) -> HttpResponseBase:
    """Resolve one QR handle to its minimal public verification result.

    Anonymous and rate-limited per probe; the response carries only the
    status, document version, digests, issuer/clinic labels and issuance
    time — never patient or clinical content, and never document bytes.
    """
    try:
        result = verify_handle(
            handle=handle, remote_addr=request.META.get("REMOTE_ADDR")
        )
    except VerificationLimitedError:
        response = render(
            request,
            "prescription/verify.html",
            {"status": "unavailable"},
            status=429,
        )
        response["Retry-After"] = "60"
        return response
    except VerificationUnavailableError:
        return render(
            request,
            "prescription/verify.html",
            {"status": "unavailable"},
            status=503,
        )
    return render(request, "prescription/verify.html", {"result": result})


@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_documents_view(request: HttpRequest) -> HttpResponseBase:
    """List and download the session patient's released documents.

    The patient session is the only authority; the ``records`` operation
    is re-validated by the database resolver on every call, and downloads
    are online access-controlled responses, never cached content.
    """
    overview = patient_session_overview()
    if overview is None or "records" not in overview.operations:
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    if request.method == "POST" and request.POST.get("action") != "download":
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    try:
        if request.method == "POST":
            result = patient_document_download(
                document_id=UUID(request.POST.get("document_id", ""))
            )
            return _attachment(result.data, result.content_type, result.file_name)
        documents = patient_documents()
    except (ValueError, ClinicalAccessDeniedError):
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    except DatabaseError:
        logger.log(logging.ERROR, "patient document download failed")
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=503,
        )
    return render(
        request,
        "prescription/patient_documents.html",
        {"documents": documents, "overview": overview},
    )


@csrf_exempt
@require_http_methods(["POST"])
def signature_callback_view(request: HttpRequest, provider: str) -> HttpResponse:
    """Authenticate and apply one provider callback; never enumerate state.

    The provider signature authenticates the raw body before any stored
    operation or tenant is resolved, so this endpoint carries no session
    and no CSRF token. Rejections are indistinguishable.
    """
    try:
        result = receive_signature_callback(
            provider=provider,
            headers=request.headers,
            body=request.body,
        )
    except SignatureCallbackError:
        return render(request, "prescription/signature_callback.html", status=403)
    if result == "rejected":
        return render(request, "prescription/signature_callback.html", status=403)
    return render(request, "prescription/signature_callback.html", status=200)
