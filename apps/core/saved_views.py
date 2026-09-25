"""Which workspace views can be saved, how they are named and reopened.

Only destinations registered here can be saved, and only with the listed
parameter values; the identity service stores them per user. A saved view is
offered and opened only while its destination is still open to the actor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.urls import Resolver404, resolve, reverse
from django.utils.translation import gettext, gettext_noop

from apps.core.navigation import DESTINATIONS, allows
from apps.scheduling.agenda_presenter import clinic_local_today

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.saved_views import SavedViewRecord

_AGENDA_VIEWS: Final = {"day": gettext_noop("Day"), "week": gettext_noop("Week")}


@dataclass(frozen=True, slots=True)
class ViewSpec:
    """A savable destination view: its key and closed parameters."""

    destination: str
    params: dict[str, str]

    def subject(self) -> str:
        """Serialize for a session token (never rendered)."""
        return json.dumps({"destination": self.destination, "params": self.params})


def spec_from_subject(subject: str) -> ViewSpec | None:
    """Parse a token subject back into a spec, or ``None`` when malformed."""
    try:
        raw = json.loads(subject)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    destination, params = raw.get("destination"), raw.get("params")
    if not isinstance(destination, str) or not isinstance(params, dict):
        return None
    return ViewSpec(destination, {str(k): str(v) for k, v in params.items()})


def is_supported(spec: ViewSpec) -> bool:
    """Report whether the destination registered exactly these parameters."""
    return spec.destination == "agenda" and (
        set(spec.params) == {"view"} and spec.params["view"] in _AGENDA_VIEWS
    )


def destination_open(
    spec: ViewSpec, *, roles: frozenset[str], granted: frozenset[str]
) -> bool:
    """Report whether the view's destination is open to the actor now."""
    place = next((item for item in DESTINATIONS if item.key == spec.destination), None)
    return place is not None and allows(place, roles=roles, granted=granted)


def spec_from_path(path: str, clinic_id: UUID) -> ViewSpec | None:
    """Return the savable view a shell page shows, or ``None``."""
    try:
        match = resolve(path)
    except Resolver404:
        return None
    if match.kwargs.get("clinic_id") != clinic_id:
        return None
    if match.view_name == "scheduling:agenda":
        return ViewSpec("agenda", {"view": "day"})
    if match.view_name == "scheduling:agenda-at":
        spec = ViewSpec("agenda", {"view": str(match.kwargs.get("view", ""))})
        return spec if is_supported(spec) else None
    return None


def view_label(spec: ViewSpec) -> str:
    """Name a saved view from the closed vocabulary: 'Agenda · Semana'."""
    view = _AGENDA_VIEWS.get(spec.params.get("view", ""), "")
    return f"{gettext('Agenda')} · {gettext(view)}"


def view_href(record: SavedViewRecord, *, clinic_id: UUID, timezone: str) -> str:
    """Reopen a saved agenda view at today's clinic-local date."""
    return reverse(
        "scheduling:agenda-at",
        args=(clinic_id, record.params["view"], clinic_local_today(timezone), 1),
    )
