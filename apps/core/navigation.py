"""Role-filtered workspace destinations: the information-architecture registry.

The shell, the command palette and the patient banner read this module; no
template hard-codes a module list. A destination is offered only when all
three hold:

1. its feature is delivered: ``url_name`` is set on the destination or on one
   of its section ``entries``. Destinations whose owning todo has not landed
   stay registered with ``url_name=None`` and are never rendered (no
   placeholder buttons);
2. the actor holds at least one of its bundle ``permission`` strings in the
   clinic, decided by ``clinic_app.has_permission`` (todo 6), on every render;
3. the actor holds a role the route's own guard accepts today
   (``route_roles``), so the shell never links to a route that would refuse.

The shell only mirrors authorization; every route keeps its own server-side
gate. Destination paths carry the clinic identifier only, never a patient.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from django.db import connection
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext, gettext_noop

from apps.identity.models import UserClinicRole
from apps.scheduling.agenda_presenter import clinic_local_today

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    type BadgeProvider = Callable[[UUID, str], str]

_ROLE = UserClinicRole.Role
# Mirrors of the legacy route guards the destinations lead to. They narrow the
# permission bundles until each owning todo moves its route to
# ``require_permission``; they never widen what a bundle grants.
STAFF_ROUTE: Final = frozenset(
    {_ROLE.OWNER, _ROLE.CLINIC_ADMIN, _ROLE.RECEPTIONIST, _ROLE.PHYSICIAN}
)
MANAGER_ROUTE: Final = frozenset({_ROLE.OWNER, _ROLE.CLINIC_ADMIN, _ROLE.RECEPTIONIST})
ADMIN_ROUTE: Final = frozenset({_ROLE.OWNER, _ROLE.CLINIC_ADMIN})

_AGENDA_READ: Final = ("appointment.read", "appointment.read_own")
_CONFIGURATION: Final = ("configuration.clinic", "configuration.organization")


def agenda_badge(clinic_id: UUID, timezone: str) -> str:  # noqa: ARG001
    """Name today's clinic-local date so the day is unambiguous across zones."""
    today = date.fromisoformat(clinic_local_today(timezone))
    return gettext("today %(day)s") % {"day": date_format(today, "d/m")}


@dataclass(frozen=True, slots=True)
class Destination:
    """One registered place or command the workspace can open.

    ``permission`` is any-of; an empty tuple means no bundle permission is
    needed (account-level pages). ``route_roles`` of ``None`` means the route
    accepts every signed-in user. ``views`` are the resolver view names that
    mark the destination current. ``entries`` are the section's
    sub-destinations (for example Availability inside Agenda).
    """

    key: str
    label_msgid: str
    url_name: str | None
    permission: tuple[str, ...]
    badge_provider: BadgeProvider | None = None
    route_roles: frozenset[str] | None = STAFF_ROUTE
    views: frozenset[str] = frozenset()
    entries: tuple[Destination, ...] = ()
    clinic_scoped: bool = True
    owner_todo: int | None = None

    @property
    def label(self) -> str:
        """Return the label in the active language."""
        return gettext(self.label_msgid)

    def section(self) -> tuple[Destination, ...]:
        """Return the delivered places of this section, the destination first."""
        own = (self,) if self.url_name is not None else ()
        return (*own, *(entry for entry in self.entries if entry.url_name))


# The ten IA destinations in their fixed order (plan annex IA). Undelivered
# ones keep their owning todo so the feature that lands them flips url_name.
DESTINATIONS: Final[tuple[Destination, ...]] = (
    Destination(
        "today",
        gettext_noop("Today"),
        None,
        _AGENDA_READ,
        owner_todo=32,
    ),
    Destination(
        "agenda",
        gettext_noop("Agenda"),
        "scheduling:agenda",
        _AGENDA_READ,
        badge_provider=agenda_badge,
        views=frozenset(
            {
                "scheduling:agenda",
                "scheduling:agenda-at",
                "scheduling:appointment-create",
                "scheduling:appointment-reschedule",
                "scheduling:appointment-cancel",
            }
        ),
        entries=(
            Destination(
                "availability",
                gettext_noop("Availability"),
                "scheduling:availability-list",
                _AGENDA_READ,
                views=frozenset(
                    {"scheduling:availability-list", "scheduling:availability-retire"}
                ),
            ),
        ),
    ),
    Destination(
        "patients",
        gettext_noop("Patients"),
        "intake:patient-list",
        ("demographics.read",),
        route_roles=MANAGER_ROUTE,
        views=frozenset(
            {
                "intake:patient-list",
                "intake:patient-create",
                "intake:patient-contacts",
                "intake:patient-access",
            }
        ),
        entries=(
            Destination(
                "consent",
                gettext_noop("Consent"),
                "consent:staff",
                ("demographics.read", *_CONFIGURATION),
                views=frozenset({"consent:staff"}),
            ),
        ),
    ),
    Destination(
        "inbox",
        gettext_noop("Clinical Inbox"),
        None,
        ("result.read", "order.observe"),
        owner_todo=32,
    ),
    Destination(
        "messages",
        gettext_noop("Messages"),
        None,
        ("demographics.read",),
        owner_todo=51,
    ),
    Destination(
        "finance",
        gettext_noop("Finance"),
        "billing:charges",
        ("charge.read",),
        route_roles=MANAGER_ROUTE,
        views=frozenset({"billing:charges", "billing:invoice"}),
    ),
    Destination(
        "operations",
        gettext_noop("Operations"),
        None,
        ("demographics.read", *_CONFIGURATION),
        owner_todo=26,
        entries=(
            Destination(
                "retention",
                gettext_noop("Retention"),
                "retention:status",
                ("demographics.read", *_CONFIGURATION),
                views=frozenset({"retention:status"}),
            ),
        ),
    ),
    Destination(
        "automations",
        gettext_noop("Automations"),
        None,
        ("automation.admin", "automation.finance", "automation.organization"),
        owner_todo=39,
    ),
    Destination(
        "reports",
        gettext_noop("Reports"),
        None,
        ("charge.read", "configuration.clinic", "configuration.organization"),
        owner_todo=63,
    ),
    Destination(
        "settings",
        gettext_noop("Settings"),
        "identity:clinic-settings",
        _CONFIGURATION,
        route_roles=ADMIN_ROUTE,
        views=frozenset({"identity:clinic-settings"}),
    ),
)

# Commands that are not places: offered by the palette only.
ACTIONS: Final[tuple[Destination, ...]] = (
    Destination(
        "new-patient",
        gettext_noop("Register a new patient"),
        "intake:patient-create",
        ("demographics.write",),
        route_roles=MANAGER_ROUTE,
    ),
    Destination(
        "preferences",
        gettext_noop("Display preferences"),
        "identity:preferences",
        (),
        route_roles=None,
        clinic_scoped=False,
    ),
)

# Patient-bound commands: POST forms carrying the enrollment in the body,
# identical to the registry row actions (templates/intake/partials).
PATIENT_ACTIONS: Final[tuple[Destination, ...]] = (
    Destination(
        "book",
        gettext_noop("Book appointment"),
        "scheduling:appointment-create",
        ("appointment.book",),
        route_roles=MANAGER_ROUTE,
    ),
    Destination(
        "contacts",
        gettext_noop("Contacts"),
        "intake:patient-contacts",
        ("demographics.write",),
        route_roles=MANAGER_ROUTE,
    ),
    Destination(
        "access",
        gettext_noop("Access"),
        "intake:patient-access",
        ("demographics.write",),
        route_roles=MANAGER_ROUTE,
    ),
)


def _all_permissions() -> frozenset[str]:
    found: set[str] = set()
    for destination in (*DESTINATIONS, *ACTIONS, *PATIENT_ACTIONS):
        for place in (destination, *destination.entries):
            found.update(place.permission)
    # The palette's patient matching (command_search) reads demographics.
    found.add("demographics.read")
    return frozenset(found)


REGISTRY_PERMISSIONS: Final = _all_permissions()


def granted_permissions(clinic_id: UUID) -> frozenset[str]:
    """Return the registry permissions the current actor holds in the clinic.

    One statement evaluates every registry permission through the todo 6
    resolver, so a revoked role or a clinic-narrowed grant hides its
    destinations on the next render. No GUC identity means nothing granted.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT candidate FROM pg_catalog.unnest(%s::text[]) AS candidate "
            "WHERE clinic_app.has_permission(candidate, %s, NULL)",
            [sorted(REGISTRY_PERMISSIONS), clinic_id],
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())


def allows(
    destination: Destination,
    *,
    roles: frozenset[str],
    granted: frozenset[str],
) -> bool:
    """Report whether one registered place is delivered and open to the actor."""
    if destination.url_name is None:
        return False
    if destination.route_roles is not None and not roles & destination.route_roles:
        return False
    return not destination.permission or bool(
        granted.intersection(destination.permission)
    )


def destination_url(destination: Destination, clinic_id: UUID) -> str:
    """Return the state-free path of a delivered place (clinic id only)."""
    if destination.url_name is None:
        msg = f"destination {destination.key} is not delivered"
        raise ValueError(msg)
    if not destination.clinic_scoped:
        return reverse(destination.url_name)
    return reverse(destination.url_name, kwargs={"clinic_id": clinic_id})


@dataclass(frozen=True, slots=True)
class NavLink:
    """One rendered destination or section entry."""

    key: str
    label: str
    url: str
    current: bool
    badge: str = ""

    @property
    def href(self) -> str:
        """Alias for the tabs component's link signature."""
        return self.url


@dataclass(frozen=True, slots=True)
class PaletteGroup:
    """One labelled group of palette rows in the combobox signature."""

    label: str
    items: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class Navigation:
    """The top-level destinations, the current section and the palette rows."""

    links: tuple[NavLink, ...]
    section: tuple[NavLink, ...]
    section_label: str
    palette: tuple[PaletteGroup, ...] = ()

    @property
    def section_current(self) -> str:
        """Return the key of the current section entry, if any."""
        return next((link.key for link in self.section if link.current), "")


def build_navigation(
    *,
    clinic_id: UUID,
    timezone: str,
    roles: frozenset[str],
    granted: frozenset[str],
    view_name: str,
) -> Navigation:
    """Resolve the visible destinations, the current one and its section.

    A destination links to its first open place: an actor who may not open
    the section's own page still reaches the entries they may open (a
    physician's Patients leads to Consent). The section row lists the open
    places only when there is more than one.
    """
    links: list[NavLink] = []
    section: tuple[NavLink, ...] = ()
    section_label = ""
    for destination in DESTINATIONS:
        open_places = [
            place
            for place in destination.section()
            if allows(place, roles=roles, granted=granted)
        ]
        if not open_places:
            continue
        current = any(view_name in place.views for place in destination.section())
        badge_provider = destination.badge_provider
        links.append(
            NavLink(
                key=destination.key,
                label=destination.label,
                url=destination_url(open_places[0], clinic_id),
                current=current,
                badge=badge_provider(clinic_id, timezone) if badge_provider else "",
            )
        )
        if current and len(open_places) > 1:
            section_label = destination.label
            section = tuple(
                NavLink(
                    key=place.key,
                    label=place.label,
                    url=destination_url(place, clinic_id),
                    current=view_name in place.views,
                )
                for place in open_places
            )
    return Navigation(
        links=tuple(links),
        section=section,
        section_label=section_label,
        palette=palette_groups(clinic_id=clinic_id, roles=roles, granted=granted),
    )


def palette_groups(
    *, clinic_id: UUID, roles: frozenset[str], granted: frozenset[str]
) -> tuple[PaletteGroup, ...]:
    """Return the palette's first rows (empty query) without another query."""
    groups: list[PaletteGroup] = []
    for label_msgid, candidates in (
        (gettext_noop("Destinations"), DESTINATIONS),
        (gettext_noop("Actions"), ACTIONS),
    ):
        items = tuple(
            {
                "value": place.url_name or "",
                "label": place.label,
                "href": destination_url(place, clinic_id),
                "kind": "destination",
            }
            for place in open_places(candidates, roles=roles, granted=granted)
        )
        if items:
            groups.append(PaletteGroup(label=gettext(label_msgid), items=items))
    return tuple(groups)


def normalized(text: str) -> str:
    """Fold case and accents so 'financas' finds 'Finanças'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def open_places(
    candidates: Iterable[Destination],
    *,
    roles: frozenset[str],
    granted: frozenset[str],
) -> tuple[Destination, ...]:
    """Return the delivered places and entries the actor may open, in order."""
    return tuple(
        place
        for destination in candidates
        for place in destination.section()
        if allows(place, roles=roles, granted=granted)
    )
