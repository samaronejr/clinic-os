"""Clinician workspace: one session's video beside its encounter notes.

The workspace is a presentation of two server-owned records. Video state
comes from the stored teleconsult session; note state comes from the EHR
services, which keep every permission and lifecycle rule. Each note action
re-validates the assigned-physician session and binds the posted version to
that session's encounter before delegating, so a session identifier never
becomes authority over another patient's record. A failed save re-renders
the physician's edits as unsaved; nothing here writes outside the services.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import DatabaseError
from django.shortcuts import render
from django.urls import reverse

from apps.ehr.finalization import amend_document, discard_draft, finalize_version
from apps.ehr.forms import AmendmentForm, DraftForm
from apps.ehr.models import ClinicalDocumentVersion, Encounter, SpecialtyTemplate
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalConflictError,
    create_draft,
    record_clinical_note,
    view_version,
)
from apps.identity.otp import flow_redirect, safe_next_url
from apps.identity.stepup import StepUpRequired
from apps.teleconsult.models import TeleconsultRoom
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    derived_state,
    patient_joined,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponseBase

    from apps.teleconsult.models import TeleconsultSession

logger = logging.getLogger(__name__)

NOTE_ACTIONS = frozenset(
    {"note-template", "note-save", "note-finalize", "note-discard", "note-amend"}
)
_SAVE_MESSAGES = {
    "stale_revision": "A versão salva mudou. Suas alterações não foram salvas. "
    "Copie o que deseja manter e reabra a versão salva.",
    "failed": "Não foi possível salvar. Suas alterações não foram salvas. "
    "Tente novamente.",
}
_CONFLICT_MESSAGES = {
    "stale_revision": "A versão salva mudou. Reabra a versão atual antes de "
    "alterar ou retificar.",
    "draft_in_progress": "Já existe um rascunho em andamento neste documento.",
    "encounter_closed": "O atendimento está encerrado; novos documentos não "
    "podem ser criados.",
    "missing_required_content": "Preencha todos os campos SOAP antes de finalizar.",
    "precondition_failed": "A versão não está no estado esperado para esta ação.",
}
_SUCCESS_MESSAGES = {
    "note-template": "Rascunho criado. As alterações só são guardadas ao salvar.",
    "note-save": "Rascunho salvo.",
    "note-finalize": "Versão finalizada. O conteúdo ficou imutável; correções "
    "usam retificação.",
    "note-discard": "Rascunho descartado.",
    "note-amend": "Retificação criada como novo rascunho. A versão anterior "
    "foi preservada.",
}
STATE_LABELS = {
    "waiting": "Aguardando",
    "active": "Em andamento",
    "ended": "Encerrada",
    "failed": "Falhou",
}
FAILURE_LABELS = {
    "encounter_closed": "O atendimento foi encerrado no registro clínico.",
    "consent_revoked": "O paciente revogou a autorização de teleconsulta.",
    "room_unavailable": "O provedor de vídeo falhou; a sala não está disponível.",
}
_STALE = "stale_revision"
_CONFLICT = 409
_UNAVAILABLE = 503
_INVALID = 400
NOTES_PARTIAL = "teleconsult/partials/clinician_notes.html"
WORKSPACE_TEMPLATE = "teleconsult/clinician.html"


def is_htmx(request: HttpRequest) -> bool:
    """Return whether the shell's htmx asked for a partial swap."""
    return request.headers.get("HX-Request") == "true"


def encounter_url(clinic_id: UUID) -> str:
    """Return the clinic's EHR encounter workspace.

    The record forms post the displayed encounter's identifier there with the
    EHR ``show`` action, which validates it and renders that record in the
    same response; no identifier travels in the URL and no later request
    resolves the destination from the shared selection.
    """
    return reverse("ehr:encounter", kwargs={"clinic_id": clinic_id})


def staff_url(clinic_id: UUID) -> str:
    """Return the clinic's teleconsult surface; every action posts there."""
    return reverse("teleconsult:staff", kwargs={"clinic_id": clinic_id})


def video_context(clinic_id: UUID, session: TeleconsultSession) -> dict[str, object]:
    """Describe the stored session for the video panel; metadata only."""
    state = derived_state(session)
    return {
        "clinic_id": clinic_id,
        "staff_url": staff_url(clinic_id),
        "record_url": encounter_url(clinic_id),
        "session": session,
        "room_name": TeleconsultRoom.objects.get(session=session).room_name,
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "failure_label": FAILURE_LABELS.get(session.failure_reason, ""),
        "patient_joined": state in ("waiting", "active") and patient_joined(session),
        "encounter": Encounter.objects.select_related("patient", "appointment").get(
            pk=session.encounter_id
        ),
    }


def notes_context(
    clinic_id: UUID,
    session: TeleconsultSession,
    *,
    message: str = "",
    error: str = "",
) -> dict[str, object]:
    """Assemble the current draft, the current finalized version or a chooser.

    The lineage is only consulted for which version is current; the version
    actually shown is read through the clinical read service, which
    authorizes the assigned physician and appends ``ehr.record.viewed`` for
    exactly that version. No clinical content is read around that boundary.
    """
    encounter = Encounter.objects.get(pk=session.encounter_id)
    lineage = ClinicalDocumentVersion.objects.filter(
        document__encounter=encounter
    ).order_by("-version")
    exposed = (
        lineage.filter(state="draft").values_list("pk", flat=True).first()
        or lineage.filter(state="finalized").values_list("pk", flat=True).first()
    )
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "staff_url": staff_url(clinic_id),
        "record_url": encounter_url(clinic_id),
        "session": session,
        "encounter": encounter,
        "message": message,
        "error": error,
        "unsaved": False,
    }
    if exposed is not None:
        version = view_version(clinic_id=clinic_id, version_id=exposed)
        context["version"] = version
        if version.state == "draft":
            context["form"] = DraftForm(version)
        else:
            context["amend_form"] = AmendmentForm(initial={"version_id": version.pk})
    elif encounter.state == Encounter.State.OPEN:
        context["templates"] = SpecialtyTemplate.objects.filter(
            clinic_id=clinic_id
        ).order_by("title", "-version")
    return context


def posted_notes_context(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> dict[str, object]:
    """Keep the draft text a native page transition carried; show it as unsaved.

    Without JavaScript, starting or ending the video submits the notes form
    itself, so the typed text travels with the transition. Nothing is written:
    the posted text is rendered over the stored draft only when it still
    targets this session's current draft, and only when it differs from the
    stored revision.
    """
    context = notes_context(clinic_id, session)
    version = context.get("version")
    if (
        not isinstance(version, ClinicalDocumentVersion)
        or version.state != "draft"
        or request.POST.get("version_id") != str(version.pk)
    ):
        return context
    form = DraftForm(version, request.POST)
    if form.is_valid() and form.content() == {
        field: getattr(version, field) for field in SOAP_FIELDS
    }:
        return context
    context.update(form=form, unsaved=True)
    return context


def _session_version(
    clinic_id: UUID, session: TeleconsultSession, raw: str
) -> ClinicalDocumentVersion:
    """Admit only a version of this session's own encounter."""
    version = view_version(clinic_id=clinic_id, version_id=UUID(raw))
    if version.document.encounter_id != session.encounter_id:
        raise TeleconsultAccessDeniedError
    return version


def _render(
    request: HttpRequest,
    context: dict[str, object],
    status: int,
    workspace: Callable[[], dict[str, object]],
) -> HttpResponseBase:
    if is_htmx(request):
        return render(request, NOTES_PARTIAL, context, status=status)
    return render(
        request, WORKSPACE_TEMPLATE, {**workspace(), **context}, status=status
    )


def _save(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> tuple[dict[str, object], int]:
    """Save explicitly; a failed save keeps the edits on screen as unsaved."""
    version = _session_version(clinic_id, session, request.POST.get("version_id", ""))
    form = DraftForm(version, request.POST)
    status = _INVALID
    if form.is_valid():
        try:
            record_clinical_note(
                clinic_id=clinic_id,
                version_id=version.pk,
                expected_revision=form.cleaned_data["revision"],
                content=form.content(),
            )
        except ClinicalConflictError:
            form.add_error(None, _SAVE_MESSAGES[_STALE])
            status = _CONFLICT
        except DatabaseError:
            # Database exception text may contain SOAP; never include exc_info.
            logger.log(
                logging.ERROR, "teleconsult note save failed; transaction rolled back"
            )
            form.add_error(None, _SAVE_MESSAGES["failed"])
            status = _UNAVAILABLE
        else:
            return notes_context(
                clinic_id, session, message=_SUCCESS_MESSAGES["note-save"]
            ), 200
    context = notes_context(clinic_id, session)
    context.update(version=version, form=form, unsaved=True)
    return context, status


def _finalize(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> tuple[dict[str, object], int] | HttpResponseBase:
    """Freeze the saved draft; the service enforces recent step-up."""
    version = _session_version(clinic_id, session, request.POST.get("version_id", ""))
    try:
        finalize_version(
            clinic_id=clinic_id,
            version_id=version.pk,
            expected_revision=int(request.POST.get("revision", "")),
            request=request,
        )
    except StepUpRequired:
        target = safe_next_url(request, staff_url(clinic_id))
        return flow_redirect(request, "identity:step-up", target)
    except ClinicalConflictError as error:
        return _conflict(clinic_id, session, error)
    return notes_context(
        clinic_id, session, message=_SUCCESS_MESSAGES["note-finalize"]
    ), 200


def _discard(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> tuple[dict[str, object], int]:
    version = _session_version(clinic_id, session, request.POST.get("version_id", ""))
    try:
        discard_draft(clinic_id=clinic_id, version_id=version.pk)
    except ClinicalConflictError as error:
        return _conflict(clinic_id, session, error)
    return notes_context(
        clinic_id, session, message=_SUCCESS_MESSAGES["note-discard"]
    ), 200


def _amend(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> tuple[dict[str, object], int]:
    version = _session_version(clinic_id, session, request.POST.get("version_id", ""))
    form = AmendmentForm(request.POST)
    if not form.is_valid():
        context = notes_context(clinic_id, session)
        context.update(version=version, amend_form=form)
        return context, _INVALID
    try:
        amend_document(
            clinic_id=clinic_id,
            version_id=version.pk,
            reason=form.cleaned_data["reason"],
        )
    except ClinicalConflictError as error:
        return _conflict(clinic_id, session, error)
    return notes_context(
        clinic_id, session, message=_SUCCESS_MESSAGES["note-amend"]
    ), 200


def _template(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> tuple[dict[str, object], int]:
    try:
        create_draft(
            clinic_id=clinic_id,
            encounter_id=session.encounter_id,
            template_id=UUID(request.POST.get("template_id", "")),
        )
    except ClinicalConflictError as error:
        return _conflict(clinic_id, session, error)
    return notes_context(
        clinic_id, session, message=_SUCCESS_MESSAGES["note-template"]
    ), 200


def _conflict(
    clinic_id: UUID, session: TeleconsultSession, error: ClinicalConflictError
) -> tuple[dict[str, object], int]:
    """Render the stored state with the fixed conflict reason; nothing changed."""
    message = _CONFLICT_MESSAGES.get(
        error.reason_code, _CONFLICT_MESSAGES["precondition_failed"]
    )
    return notes_context(clinic_id, session, error=message), _CONFLICT


_HANDLERS = {
    "note-template": _template,
    "note-save": _save,
    "note-finalize": _finalize,
    "note-discard": _discard,
    "note-amend": _amend,
}


def note_action(
    request: HttpRequest,
    clinic_id: UUID,
    session: TeleconsultSession,
    action: str,
    workspace: Callable[[], dict[str, object]],
) -> HttpResponseBase:
    """Run one explicit note action and render the notes panel or the page."""
    result = _HANDLERS[action](request, clinic_id, session)
    if not isinstance(result, tuple):
        return result
    context, status = result
    return _render(request, context, status, workspace)
