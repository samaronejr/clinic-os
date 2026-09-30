"""Assigned-physician encounter workspace with honest explicit-save feedback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import bind_patient_context, enrollment_for_patient
from apps.ehr.finalization import (
    amend_document,
    close_encounter,
    discard_draft,
    finalize_version,
)
from apps.ehr.forms import AmendmentForm, DraftForm
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    EncounterIntakeReference,
    SpecialtyTemplate,
)
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
    open_encounter,
    record_clinical_note,
    view_version,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.otp import flow_redirect, privileged_totp_required, safe_next_url
from apps.identity.stepup import StepUpRequired

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

logger = logging.getLogger(__name__)
OK = 200


def continuation(clinic_id: UUID) -> str:
    """Continue authentication without record identifiers or note text in the URL."""
    return reverse("ehr:encounter", kwargs={"clinic_id": clinic_id})


def _context(
    request: HttpRequest, clinic_id: UUID, encounter: Encounter
) -> dict[str, object]:
    """Assemble the current draft, finalized version, lineage and review panel."""
    enrollment_id = enrollment_for_patient(
        clinic_id=clinic_id, patient_id=encounter.patient_id
    )
    if enrollment_id is not None:
        bind_patient_context(
            request,
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            encounter_id=encounter.pk,
        )
    versions = list(
        ClinicalDocumentVersion.objects.filter(document__encounter=encounter)
        .select_related("template")
        .order_by("-version")
    )
    draft = next((v for v in versions if v.state == "draft"), None)
    current = next((v for v in versions if v.state == "finalized"), None)
    review = None
    selected = request.session.get(f"ehr.review.{clinic_id}")
    if selected:
        try:
            candidate = view_version(clinic_id=clinic_id, version_id=UUID(selected))
        except (ValueError, ClinicalAccessDeniedError):
            request.session.pop(f"ehr.review.{clinic_id}", None)
        else:
            if candidate.document.encounter_id == encounter.pk:
                review = candidate
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "encounter": encounter,
        "versions": versions,
        "draft": draft,
        "current": current,
        "review": review,
        "intake_references": EncounterIntakeReference.objects.filter(
            encounter=encounter
        ).select_related("submission__response__template"),
    }
    if draft is not None:
        context["version"] = draft
        context["form"] = DraftForm(draft)
    elif current is not None:
        context["version"] = current
        context["amend_form"] = AmendmentForm(initial={"version_id": current.pk})
    else:
        context["templates"] = SpecialtyTemplate.objects.filter(
            clinic_id=clinic_id
        ).order_by("title", "-version")
    return context


def _conflict(
    request: HttpRequest,
    clinic_id: UUID,
    encounter: Encounter,
    error: ClinicalConflictError,
) -> HttpResponse:
    """Render the workspace with the fixed conflict reason; nothing was written."""
    messages_by_reason = {
        "stale_revision": "A versão salva mudou. Reabra a versão atual antes de "
        "alterar ou retificar.",
        "draft_in_progress": "Já existe um rascunho em andamento neste documento.",
        "encounter_closed": "O atendimento está encerrado; novos documentos não "
        "podem ser criados.",
        "missing_required_content": "Preencha todos os campos SOAP antes de finalizar.",
        "precondition_failed": "A versão não está no estado esperado para esta ação.",
    }
    context = _context(request, clinic_id, encounter)
    context["error"] = messages_by_reason.get(
        error.reason_code, messages_by_reason["precondition_failed"]
    )
    return render(request, "ehr/encounter.html", context, status=409)


def _save(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    # The body version id prevents another tab changing the session selection
    # from silently redirecting this write to a different note.
    version = view_version(
        clinic_id=clinic_id, version_id=UUID(request.POST.get("version_id", ""))
    )
    form = DraftForm(version, request.POST)
    status = 400
    if form.is_valid():
        try:
            record_clinical_note(
                clinic_id=clinic_id,
                version_id=version.pk,
                expected_revision=form.cleaned_data["revision"],
                content=form.content(),
            )
        except ClinicalConflictError:
            form.add_error(
                None,
                "A versão salva mudou. Suas alterações não foram salvas. "
                "Copie o que deseja manter e reabra a versão salva.",
            )
            status = 409
        except DatabaseError:
            # Database exception text may contain SOAP; never include exc_info.
            logger.log(logging.ERROR, "ehr draft save failed; transaction rolled back")
            form.add_error(
                None,
                "Não foi possível salvar. Suas alterações não foram salvas. "
                "Tente novamente.",
            )
            status = 503
        else:
            request.session[key] = str(version.document.encounter_id)
            messages.success(request, "Rascunho salvo.", extra_tags="ehr.saved")
            return redirect(continuation(clinic_id))
    context = _context(request, clinic_id, version.document.encounter)
    context.update(version=version, form=form, unsaved=True)
    return render(request, "ehr/encounter.html", context, status=status)


def _finalize(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponseBase:
    """Freeze the draft; the service enforces recent step-up and audits denials."""
    version = view_version(
        clinic_id=clinic_id, version_id=UUID(request.POST.get("version_id", ""))
    )
    try:
        finalized = finalize_version(
            clinic_id=clinic_id,
            version_id=version.pk,
            expected_revision=int(request.POST.get("revision", "")),
            request=request,
        )
    except StepUpRequired:
        target = safe_next_url(request, continuation(clinic_id))
        return flow_redirect(request, "identity:step-up", target)
    except ClinicalConflictError as error:
        return _conflict(request, clinic_id, version.document.encounter, error)
    request.session[key] = str(finalized.document.encounter_id)
    messages.success(
        request,
        "Versão finalizada. O conteúdo ficou imutável; correções usam retificação.",
        extra_tags="ehr.saved",
    )
    return redirect(continuation(clinic_id))


def _amend(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    """Open the linked amendment draft; the base version is never overwritten."""
    version = view_version(
        clinic_id=clinic_id, version_id=UUID(request.POST.get("version_id", ""))
    )
    form = AmendmentForm(request.POST)
    if not form.is_valid():
        context = _context(request, clinic_id, version.document.encounter)
        context.update(version=version, amend_form=form)
        return render(request, "ehr/encounter.html", context, status=400)
    try:
        draft = amend_document(
            clinic_id=clinic_id,
            version_id=version.pk,
            reason=form.cleaned_data["reason"],
        )
    except ClinicalConflictError as error:
        return _conflict(request, clinic_id, version.document.encounter, error)
    request.session[key] = str(draft.document.encounter_id)
    messages.success(
        request,
        "Retificação criada como novo rascunho. A versão anterior foi preservada.",
        extra_tags="ehr.saved",
    )
    return redirect(continuation(clinic_id))


def _discard(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    """Retire the draft; discarded versions keep metadata and never serve content."""
    version = view_version(
        clinic_id=clinic_id, version_id=UUID(request.POST.get("version_id", ""))
    )
    try:
        discard_draft(clinic_id=clinic_id, version_id=version.pk)
    except ClinicalConflictError as error:
        return _conflict(request, clinic_id, version.document.encounter, error)
    request.session[key] = str(version.document.encounter_id)
    messages.success(request, "Rascunho descartado.", extra_tags="ehr.saved")
    return redirect(continuation(clinic_id))


def _close(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    """Close the encounter only when no live draft remains; retries are safe."""
    encounter = Encounter.objects.filter(
        pk=UUID(request.POST.get("encounter_id", "")), clinic_id=clinic_id
    ).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    try:
        closed = close_encounter(clinic_id=clinic_id, encounter_id=encounter.pk)
    except ClinicalConflictError as error:
        return _conflict(request, clinic_id, encounter, error)
    request.session[key] = str(closed.pk)
    messages.success(request, "Atendimento encerrado.", extra_tags="ehr.saved")
    return redirect(continuation(clinic_id))


def _review(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    """Select one exact version for the read-only review panel."""
    encounter = Encounter.objects.filter(
        pk=UUID(request.POST.get("encounter_id", "")), clinic_id=clinic_id
    ).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    version = view_version(
        clinic_id=clinic_id, version_id=UUID(request.POST.get("version_id", ""))
    )
    if version.document.encounter_id != encounter.pk:
        raise ClinicalAccessDeniedError
    request.session[key] = str(encounter.pk)
    request.session[f"ehr.review.{clinic_id}"] = str(version.pk)
    return redirect(continuation(clinic_id))


def _selected(clinic_id: UUID, encounter_id: UUID) -> Encounter:
    """Resolve one clinic encounter and recheck the actor's current assignment."""
    found = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if found is None:
        raise ClinicalAccessDeniedError
    # Recheck current assignment, including when the appointment was cancelled.
    return open_encounter(clinic_id=clinic_id, appointment_id=found.appointment_id)


def _show(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    """Render the posted encounter in this response; nothing resolves it again.

    A surface that displays one patient (the teleconsult workspace) posts that
    encounter's identifier and receives its record in the same response, so
    the binding validated here is the one rendered: no redirect re-reads the
    shared selection another tab may have moved in between. The selection is
    still updated so the page's own save/finalize continuations resume here.

    Rendering the record is a clinical content read: the version this response
    shows is read through the clinical read service, which authorizes the
    assigned physician and appends ``ehr.record.viewed`` for exactly that
    version, and the content rendered is the one that read returned.
    """
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    encounter = _selected(clinic_id, UUID(request.POST.get("encounter_id", "")))
    request.session[key] = str(encounter.pk)
    context = _context(request, clinic_id, encounter)
    displayed = context.get("version")
    if isinstance(displayed, ClinicalDocumentVersion):
        version = view_version(clinic_id=clinic_id, version_id=displayed.pk)
        context["version"] = version
        if version.state == "draft":
            context["form"] = DraftForm(version)
    return render(request, "ehr/encounter.html", context)


def _resume(request: HttpRequest, clinic_id: UUID, key: str) -> HttpResponse:
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    selected = request.session.get(key)
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return render(request, "ehr/encounter.html", {"clinic_id": clinic_id})
    encounter = _selected(clinic_id, UUID(selected))
    if request.method == "POST":
        if request.POST.get("action") != "template":
            raise ClinicalAccessDeniedError
        create_draft(
            clinic_id=clinic_id,
            encounter_id=encounter.pk,
            template_id=UUID(request.POST.get("template_id", "")),
        )
        return redirect(continuation(clinic_id))
    return render(
        request, "ehr/encounter.html", _context(request, clinic_id, encounter)
    )


def _dispatch_post(
    request: HttpRequest, clinic_id: UUID, key: str
) -> HttpResponseBase | None:
    """Route one POST action; ``None`` falls through to the resume handler."""
    action = request.POST.get("action")
    if action == "open":
        encounter = open_encounter(
            clinic_id=clinic_id,
            appointment_id=UUID(request.POST.get("appointment_id", "")),
        )
        request.session[key] = str(encounter.pk)
        return redirect(continuation(clinic_id))
    if action == "current":
        request.session.pop(f"ehr.review.{clinic_id}", None)
        return redirect(continuation(clinic_id))
    handlers = {
        "save": _save,
        "finalize": _finalize,
        "amend": _amend,
        "discard": _discard,
        "close": _close,
        "review": _review,
        "show": _show,
    }
    handler = handlers.get(str(action))
    return handler(request, clinic_id, key) if handler else None


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def encounter_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Open from an appointment and resume a server-session-selected exact draft."""
    key = f"ehr.encounter.{clinic_id}"
    try:
        if request.method == "POST":
            response = _dispatch_post(request, clinic_id, key)
            if response is not None:
                return response
        return _resume(request, clinic_id, key)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except (ClinicalConflictError, ValidationError):
        return render(
            request,
            "ehr/encounter.html",
            {
                "clinic_id": clinic_id,
                "error": "Não foi possível abrir este atendimento. "
                "A consulta deve estar agendada para iniciar um novo registro.",
            },
            status=409,
        )
