"""Native reception forms and a separately authenticated, read-only public display."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Final

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import connection, transaction
from django.http import Http404, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.otp import privileged_totp_required
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_lifecycle import (
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
)
from apps.scheduling.reception import (
    ReceptionAccessDeniedError,
    ReceptionConflictError,
    ReceptionInputError,
    broadcast_clinician_delay,
    call_to_room,
    issue_display_device,
    mark_arrived,
    revoke_display_device,
    update_room_status,
)
from apps.scheduling.reception_queries import (
    reception_practitioners,
    view_reception_day,
)

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from django.http import HttpRequest

TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
DEVICE_COOKIE: Final = "clinic_display_device"
DISPLAY_PATH: Final = "/waiting-display/"
COMMON: Final = frozenset({"csrfmiddlewaretoken", "intent", "day"})
ACTION_FIELDS: Final = {
    "view": frozenset(),
    "arrive": frozenset(
        {
            "appointment_id",
            "location_id",
            "expected_revision",
            "command_id",
        }
    ),
    "call": frozenset({"queue_entry_id", "room_id", "unit", "expected_revision"}),
    "room": frozenset({"room_id", "unit", "state", "expected_revision"}),
    "delay": frozenset({"practitioner_id", "minutes", "expected_revision"}),
    "issue_device": frozenset(),
    "revoke_device": frozenset({"device_id"}),
}


class ReceptionForm(forms.Form):
    """Bound each scalar and reject unknown fields before calling any service."""

    intent = forms.ChoiceField(choices=[(key, key) for key in ACTION_FIELDS])
    day = forms.DateField(input_formats=["%Y-%m-%d"], required=False)
    appointment_id = forms.UUIDField(required=False)
    location_id = forms.UUIDField(required=False)
    queue_entry_id = forms.UUIDField(required=False)
    room_id = forms.UUIDField(required=False)
    command_id = forms.UUIDField(required=False)
    practitioner_id = forms.UUIDField(required=False)
    device_id = forms.UUIDField(required=False)
    unit = forms.IntegerField(min_value=1, max_value=64, required=False)
    expected_revision = forms.IntegerField(
        min_value=0, max_value=2**31 - 1, required=False
    )
    minutes = forms.IntegerField(min_value=0, max_value=720, required=False)
    state = forms.ChoiceField(
        choices=[
            ("available", "Available"),
            ("cleaning", "Cleaning"),
            ("unavailable", "Unavailable"),
        ],
        required=False,
    )


def reception_url(clinic_id: UUID) -> str:
    """Safe continuation contains the clinic only."""
    return reverse("scheduling:reception", kwargs={"clinic_id": clinic_id})


def _render(
    request: HttpRequest,
    clinic_id: UUID,
    *,
    day: date | None = None,
    error: str = "",
    token: str = "",
) -> HttpResponseBase:
    template = (
        "scheduling/partials/reception_board.html"
        if request.headers.get("HX-Request") == "true"
        else "scheduling/reception.html"
    )
    screen = view_reception_day(clinic_id=clinic_id, day=day)
    practitioners = reception_practitioners(clinic_id=clinic_id)
    delays = {delay.practitioner_id: delay for delay in screen.delays}
    try:
        require_permission("appointment.move", clinic_id=clinic_id)
        can_manage_reception = True
    except CurrentActorError:
        can_manage_reception = False
    delay_rows = tuple(
        {
            "id": practitioner_id,
            "label": label,
            "minutes": (
                delays[practitioner_id].minutes if practitioner_id in delays else 0
            ),
            "revision": (
                delays[practitioner_id].revision if practitioner_id in delays else 0
            ),
        }
        for practitioner_id, label in practitioners
    )
    return render(
        request,
        template,
        {
            "screen": screen,
            "delay_rows": delay_rows,
            "can_manage_reception": can_manage_reception,
            "clinic_id": clinic_id,
            "action_url": reception_url(clinic_id),
            "error": error,
            "issued_token": token,
            "realtime_enabled": settings.REALTIME_ENABLED,
            "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        },
        status=409 if error else 200,
    )


def _perform(form: ReceptionForm, clinic_id: UUID) -> str:
    data = form.cleaned_data
    intent = data["intent"]
    for name in ACTION_FIELDS[intent]:
        if data.get(name) is None or data.get(name) == "":
            raise ReceptionInputError
    match intent:
        case "view":
            return ""
        case "arrive":
            mark_arrived(
                clinic_id=clinic_id,
                appointment_id=data["appointment_id"],
                location_id=data["location_id"],
                expected_revision=data["expected_revision"],
                command_id=data["command_id"],
            )
        case "call":
            call_to_room(
                clinic_id=clinic_id,
                queue_entry_id=data["queue_entry_id"],
                room_id=data["room_id"],
                room_unit=data["unit"],
                expected_revision=data["expected_revision"],
            )
        case "room":
            update_room_status(
                clinic_id=clinic_id,
                room_id=data["room_id"],
                unit=data["unit"],
                state=data["state"],
                expected_revision=data["expected_revision"],
            )
        case "delay":
            broadcast_clinician_delay(
                clinic_id=clinic_id,
                practitioner_id=data["practitioner_id"],
                minutes=data["minutes"],
                expected_revision=data["expected_revision"],
            )
        case "issue_device":
            return issue_display_device(clinic_id=clinic_id).token
        case "revoke_device":
            revoke_display_device(clinic_id=clinic_id, device_id=data["device_id"])
    return ""


@privileged_totp_required(reception_url)
@require_http_methods(["GET", "POST"])
def reception_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Run native reception forms with application-state-preserving refusals."""
    try:
        if request.method == "GET":
            return _render(request, clinic_id)
        intent = request.POST.get("intent", "")
        allowed = ACTION_FIELDS.get(intent)
        if (
            allowed is None
            or set(request.POST) - (COMMON | allowed)
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        ):
            raise Http404
        form = ReceptionForm(request.POST)
        if not form.is_valid():
            raise Http404
        # Rendering authority is checked before a write, not after committing it.
        view_reception_day(clinic_id=clinic_id, day=form.cleaned_data["day"])
        try:
            token = _perform(form, clinic_id)
        except (ReceptionConflictError, AppointmentLifecycleError) as error:
            return _render(
                request,
                clinic_id,
                day=form.cleaned_data["day"],
                error=str(error.message),
            )
        if token or intent == "view":
            return _render(
                request, clinic_id, day=form.cleaned_data["day"], token=token
            )
        messages.success(request, _("Reception updated."), extra_tags="reception")
        return _render(request, clinic_id, day=form.cleaned_data["day"])
    except (
        ReceptionAccessDeniedError,
        ReceptionInputError,
        AppointmentAccessDeniedError,
        AppointmentLifecycleInputError,
    ) as error:
        raise Http404 from error


@csrf_exempt
@require_http_methods(["GET", "POST"])
def waiting_display(request: HttpRequest) -> HttpResponseBase:
    """Authenticate device possession only; a staff cookie confers no authority."""
    raw = request.COOKIES.get(DEVICE_COOKIE, "")
    if request.method == "POST":
        raw = request.POST.get("device_token", "")
        if (
            set(request.POST) != {"device_token"}
            or len(request.POST.getlist("device_token")) != 1
        ):
            raw = ""
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        raw = authorization.removeprefix("Bearer ")
    digest = (
        hashlib.sha256(raw.encode("ascii")).hexdigest()
        if TOKEN_PATTERN.fullmatch(raw)
        else "0" * 64
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.reception_display(%s)", [digest])
        row = cursor.fetchone()
    projection = None if row is None or row[0] is None else json.loads(row[0])
    connected = projection is not None
    response = render(
        request,
        "scheduling/waiting_display.html",
        {
            "connected": connected,
            "tickets": () if projection is None else projection["tickets"],
        },
        status=200 if connected else 403,
    )
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    if connected and request.method == "POST":
        response.set_cookie(
            DEVICE_COOKIE,
            raw,
            max_age=86400,
            httponly=True,
            secure=request.is_secure(),
            samesite="Strict",
            path=DISPLAY_PATH,
        )
    return response
