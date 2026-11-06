"""Patient portal v2 switches for templates owned by other apps' views.

The patient destinations (booking, forms, teleconsult, results, charges,
documents, records, consent) render from views this app does not own, so the
``PATIENT_PORTAL_V2`` rollback setting and the session display choices reach
their templates through these tags rather than through each view's context.
They read configuration and the server-side session display choices only: no
database, record or authority input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.conf import settings

from apps.intake.portal_display import PortalDisplay, display_preferences

if TYPE_CHECKING:
    from django.template.context import Context

register = template.Library()


@register.simple_tag
def patient_portal_v2() -> bool:
    """Return whether the v2 portal layout is served (false = retained v1)."""
    return settings.PATIENT_PORTAL_V2 is True


@register.simple_tag(takes_context=True)
def patient_portal_display(context: Context) -> PortalDisplay:
    """Return the session's display choices, or the defaults without a session."""
    request = context.get("request")
    return display_preferences(getattr(request, "session", None))
