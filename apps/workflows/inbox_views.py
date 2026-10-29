"""Clinical Inbox and clinician Today pages: native forms, HTMX enhancement.

Record selectors and filters travel only in POST bodies. Authority is decided
before any session, shell or step-up work. Missing, unknown and malformed
commands and selectors get the shared refusal page and write nothing, like
every other workflow route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.orders.inbox import (
    BulkPreview,
    apply_bulk_acknowledgment,
    preview_bulk_acknowledgment,
)
from apps.orders.services import acknowledge_result
from apps.orders.values import (
    DISPOSITIONS,
    OrdersAccessDeniedError,
    OrdersConflictError,
)
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.inbox import (
    DUE_FILTERS,
    OWNER_FILTERS,
    InboxInputError,
    kind_label,
    query_inbox,
    registered_kinds,
    require_inbox,
    validated_filters,
    validated_keys,
)
from apps.workflows.today import view_today

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

    from apps.workflows.inbox import InboxFilters

ACTIONS: Final = frozenset(
    {"filter", "refresh", "acknowledge", "bulk_preview", "bulk_confirm"}
)
MAX_SELECTED: Final = 50


def inbox_url(clinic_id: UUID) -> str:
    """Return the state-free inbox path (clinic only)."""
    return reverse("workflows:inbox", kwargs={"clinic_id": clinic_id})


def today_url(clinic_id: UUID) -> str:
    """Return the state-free clinician Today path (clinic only)."""
    return reverse("workflows:today", kwargs={"clinic_id": clinic_id})


def _terms(request: HttpRequest) -> tuple[str, InboxFilters, tuple[str, ...]]:
    """Bound the command, filters and known keys; anything else is refused."""
    if request.method != "POST":
        return "", validated_filters(), ()
    action = request.POST.get("action", "")
    if action not in ACTIONS:
        raise WorkflowAccessDeniedError
    try:
        return (
            action,
            validated_filters(
                kinds=request.POST.getlist("kind"),
                owner=request.POST.get("owner", "all"),
                due=request.POST.get("due", "all"),
            ),
            validated_keys(request.POST.getlist("known")),
        )
    except InboxInputError as error:
        raise WorkflowAccessDeniedError from error


def _selected_versions(request: HttpRequest) -> tuple[UUID, ...]:
    values = request.POST.getlist("version_id")
    if len(values) > MAX_SELECTED:
        raise WorkflowAccessDeniedError
    try:
        return tuple(UUID(value) for value in values)
    except ValueError as error:
        raise WorkflowAccessDeniedError from error


def _removed_notice(removed: tuple[str, ...]) -> str:
    if not removed:
        return ""
    return ngettext(
        "One item was removed or is no longer available to you.",
        "%(count)d items were removed or are no longer available to you.",
        len(removed),
    ) % {"count": len(removed)}


def _acknowledge(request: HttpRequest, clinic_id: UUID) -> tuple[str, ...]:
    versions = _selected_versions(request)
    disposition = request.POST.get("disposition", "")
    if len(versions) != 1 or disposition not in DISPOSITIONS:
        raise InboxInputError
    acknowledge_result(
        clinic_id=clinic_id, version_id=versions[0], disposition=disposition
    )
    return (f"result:{versions[0]}",)


def _bulk_confirm(request: HttpRequest, clinic_id: UUID) -> tuple[str, ...]:
    receipts = apply_bulk_acknowledgment(
        clinic_id=clinic_id, token=request.POST.get("preview_token", "")
    )
    return tuple(f"result:{row.version_id}" for row in receipts)


@require_http_methods(["GET", "POST"])
def inbox(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide inbox authority before step-up, shell or session work."""
    try:
        require_inbox(clinic_id=clinic_id)
        return _authorized_inbox(request, clinic_id)
    except CurrentActorError:
        return render(request, "403.html", status=403)


@privileged_totp_required(inbox_url)
def _authorized_inbox(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    action, filters, known = _terms(request)
    status, outcome, error = 200, "", ""
    preview: BulkPreview | None = None
    acted: tuple[str, ...] = ()
    try:
        with transaction.atomic():
            if action == "acknowledge":
                acted = _acknowledge(request, clinic_id)
                outcome = _("Acknowledgment recorded for this result version.")
            elif action == "bulk_preview":
                selected = _selected_versions(request)
                preview = preview_bulk_acknowledgment(
                    clinic_id=clinic_id,
                    items=query_inbox(clinic_id=clinic_id, filters=filters).items,
                    version_ids=selected,
                )
            elif action == "bulk_confirm":
                acted = _bulk_confirm(request, clinic_id)
                outcome = ngettext(
                    "One normal result was acknowledged with no further action.",
                    "%(count)d normal results were acknowledged "
                    "with no further action.",
                    len(acted),
                ) % {"count": len(acted)}
    except OrdersAccessDeniedError as denied:
        raise WorkflowAccessDeniedError from denied
    except (InboxInputError, ValidationError):
        status = 400
        error = _("Choose the results and a clinical disposition, then try again.")
    except OrdersConflictError:
        status = 409
        error = _(
            "A selected result changed or is no longer eligible. "
            "Nothing was acknowledged. Review the current inbox and try again."
        )
    page = query_inbox(
        clinic_id=clinic_id,
        filters=filters,
        known=tuple(key for key in known if key not in acted),
    )
    template = (
        "workflows/partials/inbox_board.html"
        if request.headers.get("HX-Request") == "true"
        else "workflows/inbox.html"
    )
    return render(
        request,
        template,
        {
            "clinic_id": clinic_id,
            "clinic": Clinic.objects.get(pk=clinic_id),
            "page": page,
            "bulk_available": any(item.bulk_eligible for item in page.items),
            "filters": filters,
            "kinds": tuple((kind, kind_label(kind)) for kind in registered_kinds()),
            "owner_filters": OWNER_FILTERS,
            "due_filters": DUE_FILTERS,
            "preview": preview,
            "outcome": outcome,
            "error": error,
            "removed_notice": _removed_notice(page.removed),
            "realtime_enabled": settings.REALTIME_ENABLED,
            "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
def today(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Decide the own-appointment permission before step-up or shell work."""
    try:
        require_permission("appointment.read_own", clinic_id=clinic_id)
        return _authorized_today(request, clinic_id)
    except CurrentActorError:
        return render(request, "403.html", status=403)


@privileged_totp_required(today_url)
def _authorized_today(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    if request.method != "GET":
        raise WorkflowAccessDeniedError
    template = (
        "workflows/partials/today_board.html"
        if request.headers.get("HX-Request") == "true"
        else "workflows/today.html"
    )
    return render(
        request,
        template,
        {
            "clinic_id": clinic_id,
            "screen": view_today(clinic_id=clinic_id),
            "inbox_url": inbox_url(clinic_id),
            "action_url": today_url(clinic_id),
            "realtime_enabled": settings.REALTIME_ENABLED,
            "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        },
    )
