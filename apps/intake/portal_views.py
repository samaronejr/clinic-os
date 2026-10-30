"""Patient portal preferences: display choices, messaging channels and alerts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.shortcuts import render
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.intake.patient_access import patient_session_overview
from apps.intake.portal import PUSH_NOTICE, channel_preferences
from apps.intake.portal_display import (
    CONTRASTS,
    TEXT_SIZES,
    display_preferences,
    save_display_preferences,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase
    from django_stubs_ext import StrOrPromise

TEMPLATE: Final = "intake/patient_preferences.html"
PURPOSE_LABELS: Final[dict[str, StrOrPromise]] = {
    "appointment_reminder": _("Appointment reminders"),
    "booking_confirmation": _("Booking confirmations"),
    "waitlist_offer": _("Waitlist offers"),
}
CHANNEL_LABELS: Final[dict[str, StrOrPromise]] = {
    "sms": _("SMS"),
    "email": _("Email"),
    "whatsapp": _("WhatsApp"),
}
TEXT_SIZE_LABELS: Final[dict[str, StrOrPromise]] = {
    "standard": _("Standard text"),
    "large": _("Larger text"),
}
CONTRAST_LABELS: Final[dict[str, StrOrPromise]] = {
    "standard": _("Standard contrast"),
    "high": _("Higher contrast"),
}


def _refused(request: HttpRequest) -> HttpResponse:
    return render(
        request, "intake/patient_gate.html", {"state": "required"}, status=403
    )


@never_cache
@require_http_methods(["GET", "POST"])
def patient_preferences_view(request: HttpRequest) -> HttpResponseBase:
    """Show and save display choices; channel changes stay with the clinic team."""
    overview = patient_session_overview()
    if overview is None or "enrollment_view" not in overview.operations:
        return _refused(request)
    notice = error = ""
    status = 200
    if request.method == "POST":
        if request.POST.get("action", "") != "display":
            return _refused(request)
        try:
            save_display_preferences(
                request.session,
                text_size=request.POST.get("text_size", ""),
                contrast=request.POST.get("contrast", ""),
            )
        except ValueError:
            error = gettext("Choose one of the listed options.")
            status = 400
        else:
            notice = gettext("Display preferences saved for this session.")
    display = display_preferences(request.session)
    return render(
        request,
        TEMPLATE,
        {
            "display": display,
            "text_sizes": [(value, TEXT_SIZE_LABELS[value]) for value in TEXT_SIZES],
            "contrasts": [(value, CONTRAST_LABELS[value]) for value in CONTRASTS],
            "channels": [
                {
                    "purpose": PURPOSE_LABELS.get(row.purpose, row.purpose),
                    "channel": CHANNEL_LABELS.get(row.channel, row.channel),
                    "row": row,
                }
                for row in channel_preferences()
                if row.opted_in
            ],
            "consent_open": "consent" in overview.operations,
            "push_notice": PUSH_NOTICE,
            "notice": notice,
            "error": error,
        },
        status=status,
    )
