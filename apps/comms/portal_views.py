"""Patient portal messages page over todo 51's portal threads (todo 50, MSG-01).

``/patient/messages/`` lists the session subject's portal threads. Opening one and
replying are POSTs whose thread selector travels in the body, never in a URL; the
thread renders as the response, and a reload repeats the same form key, which
stores nothing twice. Every refusal renders the shared patient access gate,
whatever the selector, so a response never tells whether another patient's thread
exists. Nothing here is cached by the browser or the service worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.comms.inbox_rules import MAX_BODY_CHARS
from apps.comms.portal_messages import (
    PortalConversationClosedError,
    PortalMessageConflictError,
    PortalMessageInputError,
    patient_conversations,
    portal_message_scope,
    portal_thread,
    reply_to_portal_conversation,
)
from apps.intake.access import PatientAccessDeniedError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase
    from django_stubs_ext import StrOrPromise

    from apps.comms.portal_messages import PortalMessageScope

LIST_TEMPLATE: Final = "comms/patient_messages.html"
THREAD_TEMPLATE: Final = "comms/patient_thread.html"
# The patient may reply while the clinic keeps a thread pending, so pending reads
# as open; only closed changes what the patient can do.
STATE_LABELS: Final[dict[str, StrOrPromise]] = {
    "open": pgettext_lazy("portal conversation state", "Open"),
    "pending": pgettext_lazy("portal conversation state", "Open"),
    "closed": pgettext_lazy("portal conversation state", "Closed"),
}
SENT: Final = _("Message sent to the clinic team.")
INPUT_ERROR: Final = _(
    "Write your message before sending it. It can have up to 4,096 characters."
)
CLOSED_ERROR: Final = _(
    "The clinic team closed this conversation, so your message was not sent. "
    "Its text is below so you can keep a copy."
)
CONFLICT_ERROR: Final = _(
    "This form was already sent with a different text. Check the conversation "
    "below before writing again."
)
# Refusal -> (feedback, whether the unsent text comes back, HTTP status).
REFUSALS: Final[dict[type[Exception], tuple[StrOrPromise, bool, int]]] = {
    PortalMessageInputError: (INPUT_ERROR, True, 400),
    PortalConversationClosedError: (CLOSED_ERROR, True, 409),
    PortalMessageConflictError: (CONFLICT_ERROR, False, 409),
}


def _coverage() -> tuple[str, ...]:
    """State the clinic's configured urgent-message coverage, never more (todo 55)."""
    from apps.workflows.urgent import patient_coverage_text  # noqa: PLC0415

    return patient_coverage_text()


def _refused(request: HttpRequest) -> HttpResponse:
    """One gate for every refusal; no selector influences the bytes."""
    return render(
        request, "intake/patient_gate.html", {"state": "required"}, status=403
    )


def _selector(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def _show(  # noqa: PLR0913 - one keyword per piece of page feedback
    request: HttpRequest,
    scope: PortalMessageScope,
    conversation_id: UUID,
    *,
    notice: StrOrPromise = "",
    error: StrOrPromise = "",
    draft: str = "",
    status: int = 200,
) -> HttpResponse:
    """Render one thread of the session subject, or the gate for any other id."""
    try:
        thread = portal_thread(
            clinic_id=scope.clinic_id, conversation_id=conversation_id
        )
    except PatientAccessDeniedError:
        return _refused(request)
    conversation = thread.conversation
    return render(
        request,
        THREAD_TEMPLATE,
        {
            "scope": scope,
            "thread": thread,
            "conversation": conversation,
            "state_label": STATE_LABELS[conversation.state],
            "closed": conversation.state == "closed",
            # A fresh key per rendered form: a reload repeats this page's key.
            "reply_key": uuid4(),
            "body_max": MAX_BODY_CHARS,
            "notice": notice,
            "error": error,
            "draft": draft,
            "coverage": _coverage(),
        },
        status=status,
    )


def _reply(request: HttpRequest, scope: PortalMessageScope) -> HttpResponseBase:
    selected = _selector(request.POST.get("conversation_id", ""))
    if selected is None:
        return _refused(request)
    body = request.POST.get("body", "")
    key = _selector(request.POST.get("idempotency_key", ""))
    if key is None:
        return _show(
            request, scope, selected, error=INPUT_ERROR, draft=body, status=400
        )
    try:
        reply_to_portal_conversation(
            clinic_id=scope.clinic_id,
            conversation_id=selected,
            body=body,
            idempotency_key=key,
        )
    except PatientAccessDeniedError:
        return _refused(request)
    except (
        PortalMessageInputError,
        PortalConversationClosedError,
        PortalMessageConflictError,
    ) as refusal:
        error, keep_text, status = REFUSALS[type(refusal)]
        return _show(
            request,
            scope,
            selected,
            error=error,
            draft=body if keep_text else "",
            status=status,
        )
    return _show(request, scope, selected, notice=SENT)


@never_cache
@sensitive_post_parameters("body")
@require_http_methods(["GET", "POST"])
def patient_messages(request: HttpRequest) -> HttpResponseBase:
    """List the session subject's portal threads, open one, or reply in one."""
    scope = portal_message_scope()
    if scope is None:
        return _refused(request)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "open":
            selected = _selector(request.POST.get("conversation_id", ""))
            return (
                _refused(request)
                if selected is None
                else _show(request, scope, selected)
            )
        if action == "reply":
            return _reply(request, scope)
        return _refused(request)
    return render(
        request,
        LIST_TEMPLATE,
        {
            "scope": scope,
            "conversations": [
                {"row": row, "state_label": STATE_LABELS[row.state]}
                for row in patient_conversations()
            ],
            "coverage": _coverage(),
        },
    )
