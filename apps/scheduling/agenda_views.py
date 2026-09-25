"""Read-only clinic-local day and ISO-week agenda screen.

The route carries only the civil view, civil date, and page. Every UTC bound
comes from Todo 3 through Todo 12, so no naive midnight is ever constructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.http import Http404, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.agenda_presenter import (
    DAY_VIEW,
    AgendaScreen,
    agenda_screen,
    clinic_local_today,
    navigation,
    presented_days,
)
from apps.scheduling.appointment_forms import AGENDA_INPUT_MESSAGE
from apps.scheduling.services import (
    AgendaInputError,
    AgendaPage,
    AvailabilityAccessDeniedError,
    view_agenda,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

AGENDA_TEMPLATE: Final = "scheduling/agenda.html"


def agenda_continuation(
    clinic_id: UUID,
    view: str = DAY_VIEW,  # noqa: ARG001 - only the clinic may reach a URL
    day: str = "",  # noqa: ARG001 - only the clinic may reach a URL
    page: int = 1,  # noqa: ARG001 - only the clinic may reach a URL
) -> str:
    """Resume an agenda challenge at the default clinic agenda."""
    return reverse("scheduling:agenda", args=(clinic_id,))


def _context(
    clinic_id: UUID,
    screen: AgendaScreen,
    agenda: AgendaPage | None,
    banner: str,
    today: str,
) -> dict[str, object]:
    context: dict[str, object] = {
        "agenda": agenda,
        "banner": banner,
        "realtime_enabled": settings.REALTIME_ENABLED,
        "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        "can_manage": screen.can_manage,
        "clinic_id": clinic_id,
        "days": (),
        "timezone_key": screen.timezone_key,
        "today_url": reverse("scheduling:agenda", args=(clinic_id,)),
    }
    if agenda is not None:
        context["days"] = presented_days(agenda, can_manage=screen.can_manage)
        context.update(navigation(clinic_id, agenda, today=today))
    return context


@privileged_totp_required(agenda_continuation)
@require_http_methods(["GET"])
def agenda_view(
    request: HttpRequest,
    clinic_id: UUID,
    view: str = DAY_VIEW,
    day: str = "",
    page: int = 1,
) -> HttpResponseBase:
    """Render one authorized clinic-local day or ISO-week appointment page."""
    try:
        screen = agenda_screen(clinic_id)
        today = clinic_local_today(screen.timezone_key)
        selected_day = day or today
        try:
            agenda = view_agenda(
                clinic_id=clinic_id,
                view=view,
                date=selected_day,
                page=page,
            )
        except AgendaInputError:
            context = _context(
                clinic_id, screen, None, str(AGENDA_INPUT_MESSAGE), today
            )
        else:
            context = _context(clinic_id, screen, agenda, "", today)
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error
    return render(request, AGENDA_TEMPLATE, context)
