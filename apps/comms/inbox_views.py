"""Staff inbox pages; every state change is an explicit, keyed POST."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.comms import inbox_services as services
from apps.comms import suggestions
from apps.comms.inbox_queries import (
    CHANNEL_LABELS,
    PURPOSE_LABELS,
    PatientChoice,
    care_team_patients,
    conversation_view,
    is_clinician,
    list_conversations,
    parameters_from,
    parse_uuid,
    search_patient_choices,
    visible_purposes,
)
from apps.comms.inbox_services import (
    InboxAccessDeniedError,
    InboxConflictError,
    InboxRefusedError,
)
from apps.comms.models import QuickReply, TemplateVersion
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_search import PatientSearchInputError

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse, QueryDict

REFUSALS: Final = {
    "window_expired_template_required": _(
        "The WhatsApp 24-hour window has ended. Send an approved template."
    ),
    "template_not_approved": _("This template is not approved by the provider."),
    "template_purpose_mismatch": _(
        "The template does not fit the purpose of the conversation."
    ),
    "template_channel_mismatch": _("The template belongs to another channel."),
    "marketing_requires_template": _(
        "Marketing can only be sent with an approved template."
    ),
    "marketing_opt_in_missing": _("No marketing consent is on record."),
    "patient_not_verified": _("Link the patient through a verified contact first."),
    "conversation_closed": _("The conversation is closed."),
    "channel_purpose_mismatch": _("This channel does not accept that purpose."),
    "invalid_body": _("Review the text or the template parameters."),
    "contact_unverified": _("The patient's contact is not verified."),
    "contact_mismatch": _(
        "This conversation's number is not the patient's verified contact."
    ),
    "already_linked": _("The conversation is already linked to a patient."),
    "not_failed": _("Only a message that failed can be sent again."),
    "stale_revision": _("The conversation changed. Refresh and try again."),
    "idempotency_conflict": _("This send was already made with different content."),
    "ai_unavailable": _("Suggestion unavailable. Continue manually."),
    "ai_not_allowed": _("This message does not allow an automatic suggestion."),
    "ai_dropped": _(
        "The suggestion was discarded because it contained clinical advice."
    ),
    "invalid_scope": _("This channel and purpose cannot start a conversation."),
    "channel_not_configured": _("No provider account is configured for this channel."),
    "link_not_applicable": _("This conversation cannot be linked to a patient."),
    "invalid_state": _("That conversation state is not valid."),
}
DEFAULT_REFUSAL: Final = _("The action could not be completed.")
OPEN_CHANNELS: Final = ("whatsapp", "email", "sms", "portal")


def _refusal(code: str) -> str:
    return str(REFUSALS.get(code, DEFAULT_REFUSAL))


def _back(clinic_id: UUID, conversation_id: UUID) -> HttpResponse:
    return HttpResponseRedirect(
        reverse("comms:conversation", args=(clinic_id, conversation_id))
    )


def _find(
    request: HttpRequest, clinic_id: UUID
) -> tuple[tuple[PatientChoice, ...], str]:
    """Run the actor's own patient lookup and return its choices and a notice.

    Clinicians pick from their care team; reception searches the registry by
    exact full name. The search term travels in the POST body, never a URL.
    """
    query = request.POST.get("q", "").strip()
    try:
        if is_clinician(clinic_id):
            found = care_team_patients(clinic_id, query)
        else:
            found = search_patient_choices(clinic_id, query)
    except PatientSearchInputError:
        return (), gettext("Type at least two letters of the patient's full name.")
    except PatientAccessDeniedError:
        return (), gettext(
            "Your role cannot search patients here. Ask reception to link this number."
        )
    if not found:
        return (), gettext("No patient found for that name.")
    return found, ""


def _open_options(clinic_id: UUID) -> dict[str, object]:
    purposes = visible_purposes(clinic_id)
    return {
        "open_channels": tuple(
            (channel, str(CHANNEL_LABELS[channel])) for channel in OPEN_CHANNELS
        ),
        "open_purposes": tuple(
            (purpose, str(PURPOSE_LABELS[purpose])) for purpose in purposes
        ),
    }


@require_http_methods(["GET", "POST"])
def inbox(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """List the threads this actor may handle; unknown clinics look identical."""
    choices: tuple[PatientChoice, ...] = ()
    notice = ""
    searched = False
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "open":
                opened = services.open_conversation(
                    clinic_id=clinic_id,
                    enrollment_id=parse_uuid(request.POST.get("enrollment_id"))
                    or uuid4(),
                    channel=request.POST.get("channel", ""),
                    purpose=request.POST.get("purpose", ""),
                )
                messages.success(
                    request,
                    gettext("Conversation opened.")
                    if opened.created
                    else gettext("The conversation already exists."),
                )
                return _back(clinic_id, opened.conversation_id)
            if action == "find_patient":
                choices, notice = _find(request, clinic_id)
                searched = True
            else:
                messages.error(request, _refusal("invalid_body"))
        except InboxAccessDeniedError as error:
            raise Http404 from error
        except (InboxRefusedError, InboxConflictError) as error:
            messages.error(request, _refusal(error.code))
            return HttpResponseRedirect(reverse("comms:inbox", args=(clinic_id,)))
    state = request.GET.get("state", "open")
    try:
        rows = list_conversations(
            clinic_id=clinic_id,
            state=state,
            mine=request.GET.get("mine") == "1",
            clinician_only=request.GET.get("clinician") == "1",
        )
    except CurrentActorError as error:
        raise Http404 from error
    return render(
        request,
        "comms/inbox.html",
        {
            "clinic_id": clinic_id,
            "state": state,
            "rows": rows,
            "clinician": is_clinician(clinic_id),
            "choices": choices,
            "choice_notice": notice,
            "searched": searched,
            "realtime_enabled": settings.REALTIME_ENABLED,
            "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
            **_open_options(clinic_id),
        },
    )


def _revision(post: QueryDict) -> int:
    return int(post.get("expected_revision", "0") or 0)


def _send(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    post = request.POST
    template_id = parse_uuid(post.get("template_version_id"))
    template = (
        TemplateVersion.objects.filter(pk=template_id, clinic_id=clinic_id).first()
        if template_id
        else None
    )
    services.send_message(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        command_id=parse_uuid(post.get("command_id")) or uuid4(),
        template_version_id=template_id,
        body=None if template_id else post.get("body", ""),
        # The count comes from the stored template, never from the request.
        parameters=(
            parameters_from(post, template.parameter_count) if template else ()
        ),
    )
    return gettext("Message queued for sending.")


def _retry(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    del conversation_id
    services.retry_message(
        clinic_id=clinic_id,
        message_id=parse_uuid(request.POST.get("message_id")) or uuid4(),
        command_id=parse_uuid(request.POST.get("command_id")) or uuid4(),
    )
    return gettext("Resend queued.")


def _assign(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    services.assign_conversation(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        assignee_id=current_actor_id()
        if request.POST.get("assignee") == "me"
        else None,
        expected_revision=_revision(request.POST),
    )
    return gettext("Assignment updated.")


def _state(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    services.set_conversation_state(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        state=request.POST.get("state", ""),
        expected_revision=_revision(request.POST),
    )
    return gettext("Status updated.")


def _note(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    services.add_internal_note(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        body=request.POST.get("body", ""),
    )
    return gettext("Internal note saved.")


def _link(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    services.link_conversation_to_patient(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        enrollment_id=parse_uuid(request.POST.get("enrollment_id")) or uuid4(),
        expected_revision=_revision(request.POST),
        promote_to_care=request.POST.get("promote") == "1",
    )
    return gettext("Patient linked through a verified contact.")


def _clear(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    services.clear_clinician_flag(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        expected_revision=_revision(request.POST),
    )
    return gettext("Question taken by the clinical team.")


def _suggest(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    del request
    suggestions.request_suggestion(clinic_id=clinic_id, conversation_id=conversation_id)
    return gettext("Suggestion ready for review. Nothing was sent.")


def _use(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    del conversation_id
    suggestions.use_suggestion(
        clinic_id=clinic_id,
        suggestion_id=parse_uuid(request.POST.get("suggestion_id")) or uuid4(),
        command_id=parse_uuid(request.POST.get("command_id")) or uuid4(),
        body=request.POST.get("body", ""),
    )
    return gettext("Reviewed message queued for sending.")


def _discard(request: HttpRequest, clinic_id: UUID, conversation_id: UUID) -> str:
    del conversation_id
    suggestions.discard_suggestion(
        clinic_id=clinic_id,
        suggestion_id=parse_uuid(request.POST.get("suggestion_id")) or uuid4(),
    )
    return gettext("Suggestion discarded.")


ACTIONS: Final[dict[str, Callable[[HttpRequest, UUID, UUID], str]]] = {
    "send": _send,
    "retry": _retry,
    "assign": _assign,
    "state": _state,
    "note": _note,
    "link": _link,
    "clear_clinician": _clear,
    "suggest": _suggest,
    "use_suggestion": _use,
    "discard_suggestion": _discard,
}


def _handle(
    request: HttpRequest, clinic_id: UUID, conversation_id: UUID, action: str
) -> str:
    """Apply one named POST action and return the confirmation text."""
    handler = ACTIONS.get(action)
    if handler is None:
        raise InboxRefusedError(services.Reason.INVALID_BODY)
    return handler(request, clinic_id, conversation_id)


def _page(
    request: HttpRequest,
    clinic_id: UUID,
    conversation_id: UUID,
    extra: dict[str, object] | None = None,
) -> HttpResponse:
    try:
        view = conversation_view(
            clinic_id=clinic_id, conversation_id=conversation_id, now=timezone.now()
        )
    except CurrentActorError as error:
        raise Http404 from error
    conversation = view.conversation
    clinician = is_clinician(clinic_id)
    return render(
        request,
        "comms/conversation.html",
        {
            "clinic_id": clinic_id,
            "view": view,
            "command_id": uuid4(),
            "clinician": clinician,
            "can_link": conversation.enrollment_id is None
            and conversation.purpose != "marketing"
            and conversation.channel != "portal",
            "choices": (),
            "choice_notice": "",
            "searched": False,
            "prefill": "",
            "contacts_url": reverse("intake:patient-contacts", args=(clinic_id,)),
            "realtime_enabled": settings.REALTIME_ENABLED,
            "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
            **(extra or {}),
        },
    )


def _quick_reply_text(request: HttpRequest, clinic_id: UUID, purpose: str) -> str:
    """Return the chosen snippet of this thread's purpose; the human reviews it."""
    reply = QuickReply.objects.filter(
        pk=parse_uuid(request.POST.get("quick_reply_id")) or uuid4(),
        clinic_id=clinic_id,
        purpose=purpose,
        active=True,
    ).first()
    return "" if reply is None else str(reply.body)


@require_http_methods(["GET", "POST"])
def conversation(
    request: HttpRequest, clinic_id: UUID, conversation_id: UUID
) -> HttpResponse:
    """Show one thread; POST applies one named action then redirects."""
    if request.method == "GET":
        return _page(request, clinic_id, conversation_id)
    action = request.POST.get("action", "")
    try:
        if action == "find_patient":
            choices, notice = _find(request, clinic_id)
            return _page(
                request,
                clinic_id,
                conversation_id,
                {"choices": choices, "choice_notice": notice, "searched": True},
            )
        if action == "quick_reply":
            shown = conversation_view(
                clinic_id=clinic_id, conversation_id=conversation_id, now=timezone.now()
            )
            return _page(
                request,
                clinic_id,
                conversation_id,
                {
                    "prefill": _quick_reply_text(
                        request, clinic_id, shown.conversation.purpose
                    )
                },
            )
        confirmation = _handle(request, clinic_id, conversation_id, action)
    except InboxAccessDeniedError as error:
        raise Http404 from error
    except (InboxRefusedError, InboxConflictError) as error:
        messages.error(request, _refusal(error.code))
    except ValueError:
        messages.error(request, str(DEFAULT_REFUSAL))
    else:
        messages.success(request, confirmation)
    return _back(clinic_id, conversation_id)
