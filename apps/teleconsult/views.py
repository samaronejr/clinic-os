"""Scoped teleconsult surfaces; session identifiers travel only in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.models import ConsentAcceptance
from apps.consent.services import patient_authority
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_access import patient_session_overview
from apps.teleconsult import participants
from apps.teleconsult.hints import patient_topic
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.participants import DeviceResults, Recovery
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    TeleconsultConflictError,
    assigned_session,
    create_session,
    derived_state,
    end_consultation,
    enter_room,
    live_credential,
    open_encounters,
    patient_joined,
    patient_sessions,
    refresh_session,
    request_patient_join,
    request_physician_join,
    staff_sessions,
    start_consultation,
)
from apps.teleconsult.workspace import (
    DEVICE_FIELDS,
    FAILURE_LABELS,
    NOTE_ACTIONS,
    STATE_LABELS,
    WORKSPACE_TEMPLATE,
    device_summary,
    is_htmx,
    note_action,
    notes_context,
    posted_notes_context,
    video_context,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse, HttpResponseBase

_CONFLICT_MESSAGES = {
    "consent_required": "O paciente precisa consentir com a versão atual do "
    "texto de teleconsulta antes da sessão.",
    "encounter_closed": "O atendimento está encerrado; a sessão não pode ser criada.",
    "room_not_ready": "A sala ainda não está pronta. Aguarde a criação pelo provedor.",
    "session_closed": "Esta sessão está encerrada e não pode ser reaberta.",
    "provider_unavailable": "O provedor de vídeo não está disponível. Nenhuma "
    "sala foi criada e nenhum outro provedor foi usado no lugar.",
    "not_in_room": "Este participante não está na sala. Volte à sala de "
    "espera para entrar de novo.",
}
_DEVICE_FIELDS = ("camera", "microphone", "speaker", "network")
_JSON = "application/json"
_STATE_LABELS = STATE_LABELS
_SESSION_PARTIAL = "teleconsult/partials/clinician_session.html"
_WORKSPACE = "workspace"
_PATIENT_STATE_LABELS = {
    "waiting": "Aguardando o médico",
    "active": "Consulta em andamento",
    "ended": "Consulta encerrada",
    "failed": "Consulta indisponível",
}
_FAILURE_LABELS = {
    "encounter_closed": "O atendimento foi encerrado pela clínica.",
    "consent_revoked": "A autorização de teleconsulta foi revogada.",
    "room_unavailable": "A sala de vídeo não pôde ser criada pelo provedor.",
}
_LIVE_STATES = ("waiting", "active")
_EVENT_LABELS = {
    "created": "Sala criada",
    "joined": "Entrou na sala",
    "join_denied": "Entrada recusada",
    "started": "Consulta iniciada",
    "ended": "Consulta encerrada",
    "failed": "Consulta interrompida",
}
_ACTOR_LABELS = {"patient": "você", "physician": "médico"}
_STAFF_ACTOR_LABELS = {"patient": "paciente", "physician": "médico"}


def _private[R: HttpResponseBase](response: R) -> R:
    response["Cache-Control"] = "no-store, private"
    return response


def _wants_json(request: HttpRequest) -> bool:
    return request.headers.get("Accept") == _JSON


def _device_results(request: HttpRequest) -> DeviceResults:
    """Read closed device codes from the body; anything else raises ValueError."""
    return DeviceResults(
        *(str(request.POST.get(field, "")) for field in _DEVICE_FIELDS)
    )


def _enabled(request: HttpRequest) -> bool:
    value = request.POST.get("enabled")
    if value not in ("true", "false"):
        message = "invalid audio-only choice"
        raise ValueError(message)
    return value == "true"


def _recovery_json(recovery: Recovery) -> JsonResponse:
    """Metadata-only recovery facts; no token, identifier or clinical text."""
    return JsonResponse(
        {
            "state": recovery.state,
            "audio_only": recovery.audio_only,
            "peer_audio_only": recovery.peer_audio_only,
            "peer_present": recovery.peer_present,
            "credential_expires_in": max(
                0,
                int((recovery.credential_expires_at - timezone.now()).total_seconds()),
            ),
        }
    )


def _conflict_message(error: TeleconsultConflictError) -> str:
    return _CONFLICT_MESSAGES.get(
        error.reason_code, _CONFLICT_MESSAGES["session_closed"]
    )


def _session_rows(sessions: list[TeleconsultSession]) -> list[dict[str, object]]:
    """Render honest derived state and the scoped history for each session."""
    rows = []
    for session in sessions:
        state = derived_state(session)
        rows.append(
            {
                "session": session,
                "state": state,
                "state_label": _STATE_LABELS.get(state, state),
                "failure_label": _FAILURE_LABELS.get(session.failure_reason, ""),
                "events": _staff_events(session),
            }
        )
    return rows


def _staff_events(session: TeleconsultSession) -> list[dict[str, object]]:
    """Label the history in pt-BR; staff keep the reason code for support."""
    rows = []
    for event in session.events.order_by("created_at", "pk"):
        label = _EVENT_LABELS.get(event.kind, event.kind)
        actor = _STAFF_ACTOR_LABELS.get(event.actor_role, "")
        rows.append(
            {
                "kind": event.kind,
                "label": f"{label} ({actor})" if actor else label,
                "reason": _FAILURE_LABELS.get(event.reason_code, ""),
                "reason_code": event.reason_code,
                "at": event.created_at,
            }
        )
    return rows


def _room_context(entry_session: TeleconsultSession, role: str) -> dict[str, object]:
    """Assemble the room surface for one admitted participant."""
    room = TeleconsultRoom.objects.get(session=entry_session)
    return {
        "realtime_topic": patient_topic(patient_authority().enrollment_id),
        "realtime_enabled": settings.REALTIME_ENABLED,
        "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        "entry": {
            "session": entry_session,
            "room_name": room.room_name,
            "role": role,
            "state": derived_state(entry_session),
        },
        "events": list(entry_session.events.order_by("created_at", "pk")),
    }


def _continuation(clinic_id: UUID) -> str:
    return reverse("teleconsult:staff", kwargs={"clinic_id": clinic_id})


def _workspace_context(
    clinic_id: UUID, session: TeleconsultSession
) -> dict[str, object]:
    """Assemble identity, video and notes for the assigned physician's session."""
    context = video_context(clinic_id, session)
    context.update(notes_context(clinic_id, session))
    return context


def _workspace(
    request: HttpRequest, clinic_id: UUID, session: TeleconsultSession
) -> HttpResponse:
    """Render the whole workspace, or only the session facts for htmx.

    A native transition submits the notes form itself; the whole page keeps
    that posted text as unsaved instead of re-reading the stored draft over it.
    """
    if is_htmx(request):
        return _private(
            render(request, _SESSION_PARTIAL, video_context(clinic_id, session))
        )
    context = video_context(clinic_id, session)
    context.update(posted_notes_context(request, clinic_id, session))
    return _private(render(request, WORKSPACE_TEMPLATE, context))


def _workspace_status(clinic_id: UUID, session_id: UUID) -> JsonResponse:
    """Report metadata-only session state for the assigned physician."""
    session = refresh_session(
        assigned_session(clinic_id=clinic_id, session_id=session_id)
    )
    state = derived_state(session)
    live = state in _LIVE_STATES
    devices = participants.latest_device_check(session, "patient")
    return JsonResponse(
        {
            "state": state,
            "label": _STATE_LABELS.get(state, state),
            "reason": FAILURE_LABELS.get(session.failure_reason, ""),
            "patient_joined": live and patient_joined(session),
            "patient_removed": live and participants.patient_removed(session),
            "patient_audio_only": live and participants.audio_only(session, "patient"),
            "physician_audio_only": live
            and participants.audio_only(session, "physician"),
            "patient_devices": device_summary(devices),
        }
    )


def _workspace_conflict(
    request: HttpRequest, clinic_id: UUID, error: TeleconsultConflictError
) -> HttpResponse | None:
    """Keep a workspace request on its surface when the session conflicts."""
    if request.POST.get("surface") != _WORKSPACE:
        return None
    try:
        session = assigned_session(
            clinic_id=clinic_id, session_id=UUID(request.POST.get("session_id", ""))
        )
    except (TeleconsultAccessDeniedError, ValueError):
        return None
    context = video_context(clinic_id, session)
    message = _conflict_message(error)
    if is_htmx(request):
        context["error"] = message
        return _private(render(request, _SESSION_PARTIAL, context, status=409))
    # The notes context carries its own (empty) error; the session's goes beside it.
    context.update(posted_notes_context(request, clinic_id, session))
    context["session_error"] = message
    return _private(render(request, WORKSPACE_TEMPLATE, context, status=409))


def _staff_context(clinic_id: UUID) -> dict[str, object]:
    # The physician guard decides before any clinic lookup: an unknown clinic
    # is the same refusal as a foreign one, never a missing-row error.
    rows = _session_rows(staff_sessions(clinic_id=clinic_id))
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "clinic_timezone": Clinic.objects.get(pk=clinic_id).timezone,
        "rows": rows,
    }
    try:
        context["encounters"] = open_encounters(clinic_id=clinic_id)
    except CurrentActorError:
        context["encounters"] = []
    return context


def _join(request: HttpRequest, clinic_id: UUID, session_id: UUID) -> HttpResponse:
    """Admit the assigned physician and open the workspace for that patient."""
    issued = request_physician_join(clinic_id=clinic_id, session_id=session_id)
    entry = enter_room(token=issued.token, role=TeleconsultCredential.Role.PHYSICIAN)
    return _private(
        render(
            request, WORKSPACE_TEMPLATE, _workspace_context(clinic_id, entry.session)
        )
    )


def _note(request: HttpRequest, clinic_id: UUID, session_id: UUID) -> HttpResponseBase:
    session = assigned_session(clinic_id=clinic_id, session_id=session_id)
    # The action's own notes context is complete; the page adds the video facts.
    return _private(
        note_action(
            request,
            clinic_id,
            session,
            str(request.POST.get("action")),
            lambda: video_context(clinic_id, session),
        )
    )


def _transition(
    request: HttpRequest, clinic_id: UUID, session_id: UUID
) -> HttpResponseBase:
    """Start or end the session; a workspace request stays on its surface."""
    action = request.POST.get("action")
    transition = start_consultation if action == "start" else end_consultation
    session = transition(clinic_id=clinic_id, session_id=session_id)
    if request.POST.get("surface") == _WORKSPACE:
        return _workspace(request, clinic_id, session)
    return redirect(_continuation(clinic_id))


def _participant_action(
    request: HttpRequest,
    clinic_id: UUID,
    session_id: UUID,
    write: Callable[[], dict[str, object]],
) -> HttpResponseBase:
    """Decide every authority the reply needs, then write, then render.

    The participant guard reads only and writes nothing when it refuses. A
    page reply then reads the notes through the clinical read service, whose
    decision (and view audit) precede the participant write, so no refusal
    can follow a write the middleware would commit. htmx replies need only
    the session facts, and fetch replies only the JSON the write returns.
    """
    session = participants.clinician_session(clinic_id=clinic_id, session_id=session_id)
    if _wants_json(request):
        return _private(JsonResponse(write()))
    notes = (
        None if is_htmx(request) else posted_notes_context(request, clinic_id, session)
    )
    write()
    session = TeleconsultSession.objects.get(pk=session.pk)
    context = video_context(clinic_id, session)
    if notes is None:
        return _private(render(request, _SESSION_PARTIAL, context))
    context.update(notes)
    return _private(render(request, WORKSPACE_TEMPLATE, context))


def _staff_device_check(
    request: HttpRequest, clinic_id: UUID, session_id: UUID
) -> HttpResponseBase:
    results = _device_results(request)

    def write() -> dict[str, object]:
        participants.record_device_check(
            clinic_id=clinic_id, session_id=session_id, results=results
        )
        return {"recorded": True}

    return _participant_action(request, clinic_id, session_id, write)


def _staff_audio_only(
    request: HttpRequest, clinic_id: UUID, session_id: UUID
) -> HttpResponseBase:
    enabled = _enabled(request)

    def write() -> dict[str, object]:
        return {
            "audio_only": participants.set_audio_only(
                clinic_id=clinic_id, session_id=session_id, enabled=enabled
            )
        }

    return _participant_action(request, clinic_id, session_id, write)


def _staff_remove(
    request: HttpRequest, clinic_id: UUID, session_id: UUID
) -> HttpResponseBase:
    def write() -> dict[str, object]:
        participants.remove_patient(clinic_id=clinic_id, session_id=session_id)
        return {"removed": True}

    return _participant_action(request, clinic_id, session_id, write)


def _staff_resume(
    request: HttpRequest, clinic_id: UUID, session_id: UUID
) -> HttpResponseBase:
    del request
    return _private(
        _recovery_json(
            participants.resume_physician(clinic_id=clinic_id, session_id=session_id)
        )
    )


def _staff_post(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Route one staff POST action through the scoped service boundary."""
    action = str(request.POST.get("action"))
    if action == "create":
        create_session(
            clinic_id=clinic_id,
            encounter_id=UUID(request.POST.get("encounter_id", "")),
        )
        return redirect(_continuation(clinic_id))
    handlers = {
        "join": _join,
        "status": lambda _request, clinic, session: _private(
            _workspace_status(clinic, session)
        ),
        "start": _transition,
        "end": _transition,
        "device_check": _staff_device_check,
        "audio_only": _staff_audio_only,
        "remove": _staff_remove,
        "resume": _staff_resume,
        **dict.fromkeys(NOTE_ACTIONS, _note),
    }
    handler = handlers.get(action)
    if handler is None:
        return _private(render(request, "403.html", status=403))
    return handler(request, clinic_id, UUID(request.POST.get("session_id", "")))


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_teleconsult(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Create, join, start and end sessions for the assigned physician only."""
    try:
        if request.method == "POST":
            return _staff_post(request, clinic_id)
        return _private(
            render(request, "teleconsult/staff.html", _staff_context(clinic_id))
        )
    except TeleconsultConflictError as error:
        if _wants_json(request):
            return _private(JsonResponse({"code": error.reason_code}, status=409))
        kept = _workspace_conflict(request, clinic_id, error)
        if kept is not None:
            return kept
        context = _staff_context(clinic_id)
        context["error"] = _conflict_message(error)
        return _private(render(request, "teleconsult/staff.html", context, status=409))
    except (
        TeleconsultAccessDeniedError,
        ClinicalAccessDeniedError,
        CurrentActorError,
        ValueError,
        ValidationError,
    ):
        return _private(render(request, "403.html", status=403))


def _patient_consent(session: TeleconsultSession) -> ConsentAcceptance | None:
    """Read the bound acceptance when this patient session may see it."""
    return (
        ConsentAcceptance.objects.filter(pk=session.consent_id)
        .select_related("text")
        .first()
    )


def _physician_joined(session: TeleconsultSession) -> bool:
    """Derive the physician's presence from the scoped event history only."""
    return session.events.filter(kind="joined", actor_role="physician").exists()


def _patient_events(session: TeleconsultSession) -> list[dict[str, object]]:
    """Label the scoped history in plain language; reason codes stay hidden."""
    rows = []
    for event in session.events.order_by("created_at", "pk"):
        label = _EVENT_LABELS.get(event.kind, event.kind)
        actor = _ACTOR_LABELS.get(event.actor_role, "")
        rows.append(
            {
                "kind": event.kind,
                "label": f"{label} ({actor})" if actor else label,
                "at": event.created_at,
            }
        )
    return rows


def _patient_row(session: TeleconsultSession) -> dict[str, object]:
    """Render one session for the patient waiting room and history."""
    state = derived_state(session)
    credential = live_credential(
        session=session, role=TeleconsultCredential.Role.PATIENT
    )
    room = None
    if credential is not None and state in _LIVE_STATES:
        room = TeleconsultRoom.objects.filter(session=session).first()
    return {
        "session": session,
        "state": state,
        "state_label": _STATE_LABELS.get(state, state),
        "patient_label": _PATIENT_STATE_LABELS.get(state, state),
        "failure_label": _FAILURE_LABELS.get(session.failure_reason, ""),
        "events": _patient_events(session),
        "entered": credential is not None,
        "room_name": room.room_name if room is not None else "",
        "consent": _patient_consent(session),
        "physician_joined": state in _LIVE_STATES and _physician_joined(session),
    }


def _patient_context() -> dict[str, object]:
    rows = [_patient_row(session) for session in patient_sessions()]
    return {
        "device_url": reverse("teleconsult:patient"),
        "device_fields": DEVICE_FIELDS,
        "rows": rows,
        "live_rows": [row for row in rows if row["state"] in _LIVE_STATES],
        "past_rows": [row for row in rows if row["state"] not in _LIVE_STATES],
    }


def _patient_status(session_id: UUID) -> JsonResponse:
    """Report metadata-only room state for the bound patient's own session."""
    session = next((s for s in patient_sessions() if s.pk == session_id), None)
    if session is None:
        raise PatientAccessDeniedError
    state = derived_state(session)
    live = state in _LIVE_STATES
    return JsonResponse(
        {
            "state": state,
            "label": _PATIENT_STATE_LABELS.get(state, state),
            "reason": _FAILURE_LABELS.get(session.failure_reason, ""),
            "physician_joined": live and _physician_joined(session),
            "removed": live and participants.patient_removed(session),
            "audio_only": live and participants.audio_only(session, "patient"),
            "physician_audio_only": live
            and participants.audio_only(session, "physician"),
        }
    )


def _patient_device_check(request: HttpRequest, session_id: UUID) -> HttpResponse:
    participants.record_patient_device_check(
        session_id=session_id, results=_device_results(request)
    )
    if _wants_json(request):
        return JsonResponse({"recorded": True})
    context = _patient_context()
    context["device_message"] = True
    return render(request, "teleconsult/patient.html", context)


def _patient_post(request: HttpRequest) -> HttpResponse:
    action = request.POST.get("action")
    session_id = UUID(request.POST.get("session_id", ""))
    if action == "status":
        return _patient_status(session_id)
    if action == "device_check":
        return _patient_device_check(request, session_id)
    if action == "audio_only":
        enabled = participants.set_patient_audio_only(
            session_id=session_id, enabled=_enabled(request)
        )
        return JsonResponse({"audio_only": enabled})
    if action == "resume":
        return _recovery_json(participants.resume_patient(session_id=session_id))
    if action != "join":
        return render(request, "intake/patient_gate.html", status=403)
    issued = request_patient_join(session_id=session_id)
    entry = enter_room(token=issued.token, role=TeleconsultCredential.Role.PATIENT)
    context = _room_context(entry.session, entry.role)
    context["row"] = _patient_row(entry.session)
    return render(request, "teleconsult/patient_room.html", context)


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_teleconsult(request: HttpRequest) -> HttpResponse:
    """List this patient's sessions and admit only the bound patient.

    The ``teleconsult`` operation is re-validated from the stored session
    binding on every request; session identifiers travel only in POST bodies.
    """
    overview = patient_session_overview()
    if overview is None or "teleconsult" not in overview.operations:
        return _private(
            render(
                request,
                "intake/patient_gate.html",
                {"state": "required"},
                status=403,
            )
        )
    try:
        if request.method == "POST":
            return _private(_patient_post(request))
        return _private(render(request, "teleconsult/patient.html", _patient_context()))
    except TeleconsultConflictError as error:
        if _wants_json(request):
            return _private(JsonResponse({"code": error.reason_code}, status=409))
        context = _patient_context()
        context["error"] = _conflict_message(error)
        return _private(
            render(request, "teleconsult/patient.html", context, status=409)
        )
    except (
        TeleconsultAccessDeniedError,
        PatientAccessDeniedError,
        ValueError,
        ValidationError,
    ):
        return _private(render(request, "intake/patient_gate.html", status=403))
