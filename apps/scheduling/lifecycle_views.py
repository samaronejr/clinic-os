"""Native POST route for agenda lifecycle actions (arrive, start, complete).

Selectors travel only in the POST body. Unknown, foreign and unauthorized
appointments share the agenda's 404 refusal and write nothing; lifecycle
conflicts render a metadata-only 409 page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.http import Http404, HttpResponseBase, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.scheduling.appointment_lifecycle import (
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
    arrive,
    complete,
    start,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentIdempotencyConflictError,
)
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from django.http import HttpRequest

ACTIONS: Final = frozenset({"arrive", "start", "complete"})
CONFLICT_TEMPLATE: Final = "scheduling/lifecycle_conflict.html"
IDEMPOTENCY_MESSAGE: Final = _(
    "This request was already used for another change. Reload the agenda."
)
CONFLICT_STATUS: Final = 409
FIELDS: Final = frozenset(
    {
        "csrfmiddlewaretoken",
        "appointment_id",
        "action",
        "expected_revision",
        "command_id",
    }
)


def lifecycle_continuation(clinic_id: UUID) -> str:
    """Resume a lifecycle challenge at the clinic agenda (never replay a POST)."""
    return reverse("scheduling:agenda", args=(clinic_id,))


def _inputs(request: HttpRequest) -> tuple[str, UUID, int, UUID]:
    if set(request.POST) - FIELDS:
        raise Http404
    action = request.POST.get("action", "")
    try:
        appointment_id = UUID(request.POST.get("appointment_id", ""))
        command_id = UUID(request.POST.get("command_id", ""))
        revision = int(request.POST.get("expected_revision", ""))
    except ValueError as error:
        raise Http404 from error
    if action not in ACTIONS:
        raise Http404
    return action, appointment_id, revision, command_id


@privileged_totp_required(lifecycle_continuation)
@require_http_methods(["POST"])
def appointment_lifecycle_view(
    request: HttpRequest, clinic_id: UUID
) -> HttpResponseBase:
    """Apply one agenda lifecycle action, then return to that clinic-local day."""
    action, appointment_id, revision, command_id = _inputs(request)
    try:
        # Explicit dispatch keeps each service statically reachable (census).
        if action == "arrive":
            appointment = arrive(
                clinic_id=clinic_id,
                appointment_id=appointment_id,
                expected_revision=revision,
                command_id=command_id,
            )
        elif action == "start":
            appointment = start(
                clinic_id=clinic_id,
                appointment_id=appointment_id,
                expected_revision=revision,
                command_id=command_id,
            )
        else:
            appointment = complete(
                clinic_id=clinic_id,
                appointment_id=appointment_id,
                expected_revision=revision,
                command_id=command_id,
            )
    except (AppointmentAccessDeniedError, AppointmentLifecycleInputError) as error:
        raise Http404 from error
    except AppointmentLifecycleError as error:
        message = str(error.message)
    except AppointmentIdempotencyConflictError:
        message = str(IDEMPOTENCY_MESSAGE)
    else:
        zone = Clinic.objects.get(pk=clinic_id).timezone
        if not isinstance(zone, str):
            raise Http404
        day = format_local_minute(appointment.start_at, zone)[:10]
        target = reverse("scheduling:agenda-at", args=(clinic_id, "day", day, 1))
        redirect = HttpResponseRedirect(target)
        redirect.status_code = 303
        return redirect
    return render(
        request,
        CONFLICT_TEMPLATE,
        {
            "message": message,
            "agenda_url": reverse("scheduling:agenda", args=(clinic_id,)),
        },
        status=CONFLICT_STATUS,
    )
