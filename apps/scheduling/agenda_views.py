"""Read-only clinic-local day and ISO-week agenda screen.

The route carries only the civil view, civil date, and page. Every UTC bound
comes from Todo 3 through Todo 12, so no naive midnight is ever constructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

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
    presented_rows,
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
) -> dict[str, object]:
    context: dict[str, object] = {
        "agenda": agenda,
        "banner": banner,
        "can_manage": screen.can_manage,
        "clinic_id": clinic_id,
        "rows": (),
        "timezone_key": screen.timezone_key,
    }
    if agenda is not None:
        context["rows"] = presented_rows(agenda, can_manage=screen.can_manage)
        context.update(navigation(clinic_id, agenda))
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
        selected_day = day or clinic_local_today(screen.timezone_key)
        try:
            agenda = view_agenda(
                clinic_id=clinic_id,
                view=view,
                date=selected_day,
                page=page,
            )
        except AgendaInputError:
            context = _context(clinic_id, screen, None, AGENDA_INPUT_MESSAGE)
        else:
            context = _context(clinic_id, screen, agenda, "")
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error
    return render(request, AGENDA_TEMPLATE, context)
