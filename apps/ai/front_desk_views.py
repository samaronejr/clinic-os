"""Staff pages of the front-desk agent (todo 54): reviewed answers and handoffs.

Selectors travel in POST bodies; every page is private and no-store. Each page
decides its navigation permission before the auth decorators, as the task queue
does, and answers every other caller with the shared 403 page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop
from django.views.decorators.http import require_http_methods

from apps.ai.front_desk_kb import (
    FrontDeskConflictError,
    FrontDeskInputError,
    approve_article,
    draft_article,
    knowledge_articles,
    retire_article,
)
from apps.ai.front_desk_models import TOPICS
from apps.ai.front_desk_staff import handoff_threads, resolve_handoff
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

TOPIC_LABELS: Final = {
    "hours": gettext_noop("Opening hours"),
    "prices": gettext_noop("Prices"),
    "services": gettext_noop("Services"),
    "preparation": gettext_noop("Exam preparation"),
    "policies": gettext_noop("Administrative policies"),
}
REASON_LABELS: Final = {
    "urgent": gettext_noop("Possibly urgent message"),
    "clinical": gettext_noop("Clinical question"),
    "human_request": gettext_noop("Asked for a person"),
    "kb_missing": gettext_noop("No reviewed answer"),
    "verification_failed": gettext_noop("Identity not confirmed"),
    "policy_limit": gettext_noop("Outside the assistant's limits"),
}
STATE_LABELS: Final = {
    "draft": gettext_noop("Draft"),
    "approved": gettext_noop("Approved"),
    "retired": gettext_noop("No longer used"),
}
ARTICLE_INPUT: Final = gettext_noop(
    "Use a short key (lowercase letters, digits, hyphens), a title, an answer of "
    "up to 600 characters without e-mail addresses, links or long numbers, up to "
    "12 keywords and, optionally, one plain https link."
)
CONFLICT: Final = gettext_noop(
    "This item changed since you opened the page. Review it again before deciding."
)
_MAX_DIGITS: Final = 5
BAD_REQUEST: Final = 400


def _forbidden(request: HttpRequest) -> HttpResponse:
    return render(request, "403.html", status=403)


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "private, no-store"
    return response


def _selected(request: HttpRequest, name: str) -> tuple[UUID, int] | None:
    raw = request.POST.get("revision", "")
    try:
        selector = UUID(request.POST.get(name, ""))
    except ValueError:
        return None
    if not raw.isdecimal() or len(raw) > _MAX_DIGITS:
        return None
    return selector, int(raw)


def _knowledge_url(clinic_id: UUID) -> str:
    return reverse("ai:front-desk-knowledge", args=(clinic_id,))


def front_desk_knowledge_page(
    request: HttpRequest, clinic_id: UUID
) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any session work."""
    try:
        require_permission("configuration.clinic", clinic_id=clinic_id)
        return _front_desk_knowledge(request, clinic_id)
    except CurrentActorError:
        return _forbidden(request)


def _article_command(request: HttpRequest, clinic_id: UUID) -> tuple[str, str, int]:
    """Apply one article command; return (notice, error, status)."""
    action = request.POST.get("action", "")
    if action == "draft":
        try:
            draft_article(
                clinic_id=clinic_id,
                key=request.POST.get("key", "").strip(),
                topic=request.POST.get("topic", ""),
                title=request.POST.get("title", ""),
                body=request.POST.get("body", ""),
                keywords=tuple(request.POST.get("keywords", "").split(",")),
                link=request.POST.get("link", "").strip(),
            )
        except FrontDeskInputError:
            return "", _(ARTICLE_INPUT), 400
        return (
            _("Draft saved. Approve it to let the assistant answer with it."),
            "",
            200,
        )
    selected = _selected(request, "article_id")
    if selected is None or action not in ("approve", "retire"):
        return "", "", 400
    article_id, revision = selected
    try:
        if action == "approve":
            approve_article(
                clinic_id=clinic_id, article_id=article_id, expected_revision=revision
            )
        else:
            retire_article(
                clinic_id=clinic_id, article_id=article_id, expected_revision=revision
            )
    except FrontDeskConflictError:
        return "", _(CONFLICT), 409
    notice = (
        _("Answer approved. The assistant now uses this version.")
        if action == "approve"
        else _("Answer retired. The assistant no longer uses it.")
    )
    return notice, "", 200


@privileged_totp_required(_knowledge_url)
@require_http_methods(["GET", "POST"])
def _front_desk_knowledge(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Draft, approve and retire the clinic's reviewed administrative answers."""
    require_permission("configuration.clinic", clinic_id=clinic_id)
    notice, error, status = "", "", 200
    if request.method == "POST":
        notice, error, status = _article_command(request, clinic_id)
        if status == BAD_REQUEST and not error:
            return _private(render(request, "400.html", status=400))
    articles = knowledge_articles(clinic_id=clinic_id)
    return _private(
        render(
            request,
            "ai/front_desk_knowledge.html",
            {
                "clinic": Clinic.objects.get(pk=clinic_id),
                "articles": [
                    {
                        "row": article,
                        "topic": _(TOPIC_LABELS[article.topic]),
                        "state": _(STATE_LABELS[article.state]),
                    }
                    for article in articles
                ],
                "topics": [(topic, _(TOPIC_LABELS[topic])) for topic in TOPICS],
                "notice": notice,
                "error": error,
            },
            status=status,
        )
    )


def _handoffs_url(clinic_id: UUID) -> str:
    return reverse("ai:front-desk-handoffs", args=(clinic_id,))


def front_desk_handoffs_page(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any session work."""
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
        return _front_desk_handoffs(request, clinic_id)
    except CurrentActorError:
        return _forbidden(request)


@privileged_totp_required(_handoffs_url)
@require_http_methods(["GET", "POST"])
def _front_desk_handoffs(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """List the conversations the assistant handed to a person; close one."""
    require_permission("demographics.read", clinic_id=clinic_id)
    notice, error, status = "", "", 200
    if request.method == "POST":
        selected = _selected(request, "thread_id")
        if selected is None:
            return _private(render(request, "400.html", status=400))
        thread_id, revision = selected
        try:
            resolve_handoff(
                clinic_id=clinic_id, thread_id=thread_id, expected_revision=revision
            )
        except FrontDeskConflictError:
            error, status = _(CONFLICT), 409
        else:
            notice = _("Marked as resolved. The assistant answers new messages again.")
    rows = handoff_threads(clinic_id=clinic_id)
    return _private(
        render(
            request,
            "ai/front_desk_handoffs.html",
            {
                "clinic": Clinic.objects.get(pk=clinic_id),
                "rows": [
                    {"row": row, "reason": _(REASON_LABELS[row.reason])} for row in rows
                ],
                "notice": notice,
                "error": error,
            },
            status=status,
        )
    )
