"""Staff retention status workspace and the patient released-records surface."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.intake.models import Patient
from apps.intake.patient_access import patient_session_overview
from apps.retention.forms import (
    DisposalForm,
    ExportForm,
    HoldForm,
    HoldReleaseForm,
    PolicyForm,
    ReleaseForm,
    ReleaseRevokeForm,
)
from apps.retention.models import (
    RECORD_CLASSES,
    LegalHold,
    RecordExport,
    RecordRelease,
    RetentionPolicy,
)
from apps.retention.services import (
    POLICY_ROLES,
    STAFF_ROLES,
    RetentionAccessDeniedError,
    RetentionConflictError,
    approve_policy,
    export_patient_records,
    export_staff_records,
    patient_released_records,
    place_hold,
    propose_policy,
    release_hold,
    release_version,
    request_disposal,
    retire_policy,
    revoke_release,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

logger = logging.getLogger(__name__)
OK = 200


def continuation(clinic_id: UUID) -> str:
    """Authenticate without record identifiers in the continuation URL."""
    return reverse("retention:status", kwargs={"clinic_id": clinic_id})


def _is_manager(clinic_id: UUID) -> bool:
    try:
        actor = current_actor_id()
    except CurrentActorError:
        return False
    return UserClinicRole.objects.filter(
        user_id=actor, clinic_id=clinic_id, role__in=POLICY_ROLES
    ).exists()


def _is_physician(clinic_id: UUID) -> bool:
    try:
        actor = current_actor_id()
    except CurrentActorError:
        return False
    return UserClinicRole.objects.filter(
        user_id=actor, clinic_id=clinic_id, role=UserClinicRole.Role.PHYSICIAN
    ).exists()


def _context(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    """Assemble the status panels; every list is already RLS-scoped."""
    del request
    manager = _is_manager(clinic_id)
    physician = _is_physician(clinic_id)
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "can_manage": manager,
        "can_release": physician,
        "record_classes": RECORD_CLASSES,
        "policies": RetentionPolicy.objects.filter(clinic_id=clinic_id).order_by(
            "record_class", "-version"
        ),
        "holds": LegalHold.objects.filter(clinic_id=clinic_id).order_by("-created_at"),
        "releases": RecordRelease.objects.filter(clinic_id=clinic_id)
        .select_related("patient")
        .order_by("-created_at"),
        "exports": RecordExport.objects.filter(clinic_id=clinic_id)
        .select_related("patient")
        .order_by("-created_at")[:20],
    }
    if manager:
        context["policy_form"] = PolicyForm()
        context["hold_form"] = HoldForm()
        context["hold_release_form"] = HoldReleaseForm()
        context["disposal_form"] = DisposalForm()
    if physician:
        released = RecordRelease.objects.filter(
            clinic_id=clinic_id, revoked_at__isnull=True
        ).values_list("version_id", flat=True)
        context["releasable"] = (
            ClinicalDocumentVersion.objects.filter(
                document__encounter__clinic_id=clinic_id,
                state__in=("finalized", "superseded"),
            )
            .exclude(pk__in=released)
            .select_related("document__encounter__patient")
            .order_by("-finalized_at")[:50]
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT patient_id FROM clinic_app.retention_care_patients(%s)",
                [str(clinic_id)],
            )
            care_patient_ids = [row[0] for row in cursor.fetchall()]
        # Names are tenant envelopes: ordering happens after decryption.
        context["export_patients"] = sorted(
            Patient.objects.filter(pk__in=care_patient_ids),
            key=lambda patient: (patient.full_name.lower(), str(patient.pk)),
        )
    return context


def _render(
    request: HttpRequest, clinic_id: UUID, *, status: int = OK, **extra: object
) -> HttpResponse:
    context = _context(request, clinic_id)
    context.update(extra)
    return render(request, "retention/status.html", context, status=status)


def _conflict(
    request: HttpRequest, clinic_id: UUID, error: RetentionConflictError
) -> HttpResponse:
    """Render the workspace with the fixed conflict reason; nothing was written."""
    messages_by_reason = {
        "precondition_failed": "O registro não está no estado esperado para esta ação.",
        "export_too_large": "O pacote excede o limite de registros.",
    }
    return _render(
        request,
        clinic_id,
        status=409,
        error=messages_by_reason.get(
            error.reason_code, messages_by_reason["precondition_failed"]
        ),
    )


def _propose(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = PolicyForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400, policy_form=form)
    try:
        propose_policy(
            clinic_id=clinic_id,
            record_class=form.cleaned_data["record_class"],
            retention_days=form.cleaned_data["retention_days"],
        )
    except ValidationError as error:
        form.add_error(None, error)
        return _render(request, clinic_id, status=400, policy_form=form)
    messages.success(request, "Política de retenção proposta.")
    return redirect(continuation(clinic_id))


def _approve(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    try:
        approve_policy(
            clinic_id=clinic_id,
            policy_id=UUID(request.POST.get("policy_id", "")),
        )
    except RetentionConflictError as error:
        return _conflict(request, clinic_id, error)
    messages.success(request, "Política aprovada.")
    return redirect(continuation(clinic_id))


def _retire(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    retire_policy(
        clinic_id=clinic_id, policy_id=UUID(request.POST.get("policy_id", ""))
    )
    messages.success(request, "Política retirada.")
    return redirect(continuation(clinic_id))


def _hold(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = HoldForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400, hold_form=form)
    try:
        place_hold(
            clinic_id=clinic_id,
            record_class=form.cleaned_data["record_class"],
            record_id=form.cleaned_data["record_id"],
            authority=form.cleaned_data["authority"],
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as error:
        form.add_error(None, error)
        return _render(request, clinic_id, status=400, hold_form=form)
    messages.success(request, "Guarda legal aplicada.")
    return redirect(continuation(clinic_id))


def _release_hold(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = HoldReleaseForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400, hold_release_form=form)
    try:
        release_hold(
            clinic_id=clinic_id,
            hold_id=form.cleaned_data["hold_id"],
            authority=form.cleaned_data["release_authority"],
            reason=form.cleaned_data["release_reason"],
        )
    except ValidationError as error:
        form.add_error(None, error)
        return _render(request, clinic_id, status=400, hold_release_form=form)
    messages.success(request, "Guarda legal liberada.")
    return redirect(continuation(clinic_id))


def _release(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = ReleaseForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400)
    try:
        release_version(clinic_id=clinic_id, version_id=form.cleaned_data["version_id"])
    except ClinicalConflictError:
        return _render(
            request,
            clinic_id,
            status=409,
            error="A versão precisa estar finalizada para ser liberada.",
        )
    messages.success(request, "Versão liberada para o paciente.")
    return redirect(continuation(clinic_id))


def _revoke(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = ReleaseRevokeForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400)
    revoke_release(clinic_id=clinic_id, release_id=form.cleaned_data["release_id"])
    messages.success(request, "Liberação revogada.")
    return redirect(continuation(clinic_id))


def _export(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = ExportForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400)
    try:
        package = export_staff_records(
            clinic_id=clinic_id, patient_id=form.cleaned_data["patient_id"]
        )
    except RetentionConflictError as error:
        return _conflict(request, clinic_id, error)
    response = HttpResponse(package.data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{package.file_name}"'
    response["Content-Length"] = str(len(package.data))
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


def _disposal(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    form = DisposalForm(request.POST)
    if not form.is_valid():
        return _render(request, clinic_id, status=400, disposal_form=form)
    try:
        decision = request_disposal(
            clinic_id=clinic_id,
            record_class=form.cleaned_data["record_class"],
            record_id=form.cleaned_data["record_id"],
        )
    except RetentionAccessDeniedError:
        return _render(
            request,
            clinic_id,
            status=403,
            error="Descarte negado: o registro está retido ou sem política "
            "aprovada. Nada foi removido.",
        )
    eligible = (
        decision.eligible_at.strftime("%d/%m/%Y %H:%M")
        if decision.eligible_at is not None
        else ""
    )
    return _render(
        request,
        clinic_id,
        disposal=(
            f"Elegível desde {eligible}. Nenhuma remoção automática "
            "existe; a eliminação exige um processo aprovado."
        ),
    )


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def retention_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Show retention status and handle the explicit lifecycle actions."""
    try:
        require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
        if request.method == "POST":
            action = request.POST.get("action")
            handlers = {
                "propose_policy": _propose,
                "approve_policy": _approve,
                "retire_policy": _retire,
                "place_hold": _hold,
                "release_hold": _release_hold,
                "release": _release,
                "revoke_release": _revoke,
                "export": _export,
                "check_disposal": _disposal,
            }
            handler = handlers.get(str(action))
            if handler is None:
                return render(request, "403.html", status=403)
            return handler(request, clinic_id)
        return _render(request, clinic_id)
    except (ValueError, CurrentActorError, RetentionAccessDeniedError):
        return render(request, "403.html", status=403)
    except ClinicalAccessDeniedError:
        return render(request, "403.html", status=403)
    except DatabaseError:
        logger.log(logging.ERROR, "retention action failed; rolled back")
        return _render(
            request,
            clinic_id,
            status=503,
            error="Não foi possível concluir a ação. Nada foi alterado.",
        )


@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_records(request: HttpRequest) -> HttpResponseBase:
    """List and export the session patient's released records.

    The patient session is the only authority; the ``records`` operation is
    re-validated by the database resolver on every call, and exports are
    online access-controlled downloads, never cached content.
    """
    overview = patient_session_overview()
    if overview is None or "records" not in overview.operations:
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    try:
        if request.method == "POST":
            if request.POST.get("action") != "export":
                return render(
                    request,
                    "intake/patient_gate.html",
                    {"state": "required"},
                    status=403,
                )
            package = export_patient_records()
            response = HttpResponse(package.data, content_type="application/zip")
            response["Content-Disposition"] = (
                f'attachment; filename="{package.file_name}"'
            )
            response["Content-Length"] = str(len(package.data))
            response["X-Content-Type-Options"] = "nosniff"
            response["Cache-Control"] = "no-store"
            return response
        records = patient_released_records()
    except (RetentionAccessDeniedError, RetentionConflictError):
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    except DatabaseError:
        logger.log(logging.ERROR, "patient records export failed; rolled back")
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=503,
        )
    return render(
        request,
        "retention/patient_records.html",
        {"records": records, "overview": overview},
    )
