"""Staff pages for todo 55: urgent concerns, coverage policy and release notices.

Selectors travel in POST bodies; every page is private and no-store. Each page
decides its navigation permission before the auth decorators, as the task queue
does, and answers every other caller with the shared 403 page, whatever the
selector.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop
from django.views.decorators.http import require_http_methods

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.workflows.release import (
    CLINIC_GATE,
    current_notice,
    publish_release_notice,
)
from apps.workflows.release_models import MAX_NOTICE_CHARS, ReleaseNoticeTemplate
from apps.workflows.urgent import (
    WEEKDAY_LABELS,
    acknowledge_urgent_concern,
    coverage_settings,
    coverage_text,
    current_coverage,
    resolve_urgent_concern,
    set_urgent_coverage,
    urgent_concerns,
)
from apps.workflows.urgent_models import MAX_RESPONSE_HOURS, RESPONSIBLE_ROLES
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

ROLE_LABELS: Final = {
    "nurse": gettext_noop("Nurse"),
    "physician": gettext_noop("Physician"),
    "receptionist": gettext_noop("Receptionist"),
    "clinic_manager": gettext_noop("Clinic manager"),
}
_MAX_DIGITS: Final = 5
_CLOCK_CHARS: Final = 5
COVERAGE_INPUT: Final = (
    "Choose at least one day and one responsible role, an end time after the "
    "start time and a response window of 1 to 72 hours."
)
CONFLICT: Final = (
    "This item changed since you opened the page. Review it again before deciding."
)


def _forbidden(request: HttpRequest) -> HttpResponse:
    return render(request, "403.html", status=403)


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "private, no-store"
    return response


def _uuid(request: HttpRequest, name: str) -> UUID | None:
    try:
        return UUID(request.POST.get(name, ""))
    except ValueError:
        return None


def _int(request: HttpRequest, name: str) -> int | None:
    raw = request.POST.get(name, "")
    return int(raw) if raw.isdecimal() and len(raw) <= _MAX_DIGITS else None


def _urgent_url(clinic_id: UUID) -> str:
    return reverse("workflows:urgent-concerns", args=(clinic_id,))


def urgent_concerns_page(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any session work."""
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
        return _urgent_concerns(request, clinic_id)
    except CurrentActorError:
        return _forbidden(request)


@privileged_totp_required(_urgent_url)
@require_http_methods(["GET", "POST"])
def _urgent_concerns(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """List the actor's urgent concerns; acknowledge or resolve one."""
    require_permission("demographics.read", clinic_id=clinic_id)
    notice = error = ""
    status = 200
    if request.method == "POST":
        concern = _uuid(request, "concern_id")
        revision = _int(request, "revision")
        action = request.POST.get("action", "")
        # An unknown action or a malformed selector is the shared refusal, like
        # an unknown record: the same 403 page every workflow route answers.
        if concern is None or revision is None or action not in ("ack", "resolve"):
            return _forbidden(request)
        try:
            if action == "ack":
                acknowledge_urgent_concern(
                    clinic_id=clinic_id, concern_id=concern, expected_revision=revision
                )
            else:
                resolve_urgent_concern(
                    clinic_id=clinic_id, concern_id=concern, expected_revision=revision
                )
        except CurrentActorError:
            return _forbidden(request)
        except WorkflowConflictError:
            error, status = _(CONFLICT), 409
        else:
            notice = (
                _("Concern acknowledged. Reply to the patient in the conversation.")
                if action == "ack"
                else _("Concern resolved.")
            )
    try:
        rows = urgent_concerns(clinic_id=clinic_id)
    except CurrentActorError:
        return _forbidden(request)
    clinic = Clinic.objects.get(pk=clinic_id)
    return _private(
        render(
            request,
            "workflows/urgent_concerns.html",
            {
                "clinic": clinic,
                "rows": rows,
                "notice": notice,
                "error": error,
                "coverage": coverage_text(current_coverage(clinic_id=clinic_id)),
            },
            status=status,
        )
    )


def _coverage_url(clinic_id: UUID) -> str:
    return reverse("workflows:urgent-coverage", args=(clinic_id,))


def _clock(raw: str) -> time | None:
    try:
        return time.fromisoformat(raw) if len(raw) == _CLOCK_CHARS else None
    except ValueError:
        return None


def _coverage_terms(
    request: HttpRequest,
) -> tuple[int, int, time, time, tuple[int, ...], tuple[str, ...]] | None:
    expected = _int(request, "expected_version")
    hours = _int(request, "response_hours")
    opens = _clock(request.POST.get("opens_at", ""))
    closes = _clock(request.POST.get("closes_at", ""))
    days = request.POST.getlist("weekdays")
    if (
        expected is None
        or hours is None
        or opens is None
        or closes is None
        or not all(day.isdecimal() and len(day) == 1 for day in days)
    ):
        return None
    return (
        expected,
        hours,
        opens,
        closes,
        tuple(int(day) for day in days),
        tuple(request.POST.getlist("responsible_roles")),
    )


def urgent_coverage_page(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any session work."""
    try:
        require_permission("configuration.clinic", clinic_id=clinic_id)
        return _urgent_coverage(request, clinic_id)
    except CurrentActorError:
        return _forbidden(request)


@privileged_totp_required(_coverage_url)
@require_http_methods(["GET", "POST"])
def _urgent_coverage(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Show and publish the clinic's urgent-concern coverage policy."""
    require_permission("configuration.clinic", clinic_id=clinic_id)
    notice = error = ""
    status = 200
    if request.method == "POST":
        if request.POST.get("action", "") != "publish":
            return _forbidden(request)
        terms = _coverage_terms(request)
        error, status = _(COVERAGE_INPUT), 400
        if terms is not None:
            expected, hours, opens, closes, days, roles = terms
            try:
                set_urgent_coverage(
                    clinic_id=clinic_id,
                    expected_version=expected,
                    weekdays=days,
                    opens_at=opens,
                    closes_at=closes,
                    response_hours=hours,
                    responsible_roles=roles,
                )
            except CurrentActorError:
                return _forbidden(request)
            except WorkflowInputError:
                pass
            except WorkflowConflictError:
                error, status = _(CONFLICT), 409
            else:
                error, status = "", 200
                notice = _("Coverage published. Patients now see these hours.")
    coverage = coverage_settings(clinic_id=clinic_id)
    return _private(
        render(
            request,
            "workflows/urgent_coverage.html",
            {
                "clinic": Clinic.objects.get(pk=clinic_id),
                "coverage": coverage,
                "patient_text": coverage_text(coverage),
                "weekdays": [
                    (index, _(label)) for index, label in enumerate(WEEKDAY_LABELS)
                ],
                "roles": [(role, _(ROLE_LABELS[role])) for role in RESPONSIBLE_ROLES],
                "max_hours": MAX_RESPONSE_HOURS,
                "notice": notice,
                "error": error,
            },
            status=status,
        )
    )


def _notices_url(clinic_id: UUID) -> str:
    return reverse("workflows:release-notices", args=(clinic_id,))


def release_notices_page(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any session work."""
    try:
        require_permission(CLINIC_GATE, clinic_id=clinic_id)
        return _release_notices(request, clinic_id)
    except CurrentActorError:
        return _forbidden(request)


@privileged_totp_required(_notices_url)
@require_http_methods(["GET", "POST"])
def _release_notices(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Approve the wording patients receive when a result is released."""
    require_permission(CLINIC_GATE, clinic_id=clinic_id)
    notice = error = ""
    status = 200
    if request.method == "POST":
        if request.POST.get("action", "") != "publish":
            return _forbidden(request)
        try:
            publish_release_notice(
                clinic_id=clinic_id, body=request.POST.get("body", "")
            )
        except CurrentActorError:
            return _forbidden(request)
        except WorkflowInputError:
            error, status = (
                _(
                    "Write the notice in up to 300 characters, without numbers or "
                    "e-mail addresses: it is the same text for every patient."
                ),
                400,
            )
        else:
            notice = _("Notice approved. New releases use this version.")
    current = current_notice(clinic_id=clinic_id)
    history = ReleaseNoticeTemplate.objects.filter(clinic_id=clinic_id).order_by(
        "-version"
    )[:10]
    return _private(
        render(
            request,
            "workflows/release_notices.html",
            {
                "clinic": Clinic.objects.get(pk=clinic_id),
                "current": current,
                "history": history,
                "max_chars": MAX_NOTICE_CHARS,
                "notice": notice,
                "error": error,
            },
            status=status,
        )
    )
