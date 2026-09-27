"""HTTP permission boundary for the delivered workspace registry.

Domain services retain their narrower role, record and step-up guards. This
boundary makes the registry's permission contract effective on direct requests,
not only on navigation rendering. Unknown and forbidden destinations share the
existing domain denial surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.shortcuts import render

from apps.core.navigation import DESTINATIONS, granted_permissions
from apps.core.patient_context import PATIENT_BOUND_VIEWS, patient_context_allowed

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def workspace_denial(request: HttpRequest) -> HttpResponse | None:
    """Refuse a delivered destination or patient page before its view executes."""
    match = request.resolver_match
    if match is None:
        return None
    clinic_id = match.kwargs.get("clinic_id")
    if not isinstance(clinic_id, UUID):
        return None
    places = tuple(
        place
        for destination in DESTINATIONS
        for place in destination.section()
        if match.view_name == place.url_name or match.view_name in place.views
    )
    patient_page = match.view_name in PATIENT_BOUND_VIEWS
    if not places and not patient_page:
        return None
    granted = granted_permissions(clinic_id)
    denied = any(not granted.intersection(place.permission) for place in places)
    if patient_page and not patient_context_allowed(request, clinic_id=clinic_id):
        denied = True
    if not denied:
        return None
    status = 404 if match.namespace in {"scheduling", "intake"} else 403
    return render(request, f"{status}.html", status=status)
