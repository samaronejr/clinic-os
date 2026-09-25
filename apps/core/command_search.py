"""Command palette search: allowed destinations, actions and exact patient matches.

Destinations and actions come from the ``apps.core.navigation`` registry and
pass the same permission-bundle and route-guard filter as the shell. Patient
matching is exact only: the full name must equal a registry name (case and
spacing folded), so partial words never reveal who is registered. It runs
through the audited registry service with its own clinic-manager check, one
full name at a time, and stops with ``refine`` instead of returning a partial
answer when too many similar names share the typed text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from django.utils.translation import gettext, gettext_noop

from apps.core.navigation import (
    ACTIONS,
    DESTINATIONS,
    MANAGER_ROUTE,
    Destination,
    destination_url,
    granted_permissions,
    normalized,
    open_places,
)
from apps.core.patient_context import DEMOGRAPHICS_READ, age_label
from apps.core.saved_views import (
    ViewSpec,
    destination_open,
    is_supported,
    spec_from_path,
    view_href,
    view_label,
)
from apps.identity.saved_views import list_saved_views
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_search import (
    MAX_QUERY_LENGTH,
    PatientSearchInputError,
    search_patients,
)
from apps.scheduling.agenda_presenter import clinic_local_today

if TYPE_CHECKING:
    from uuid import UUID

MAX_COMMAND_QUERY: Final = MAX_QUERY_LENGTH
MAX_PATIENT_PAGES: Final = 4
MAX_PATIENT_MATCHES: Final = 5
MIN_NAME_WORDS: Final = 2
GROUP_DESTINATIONS: Final = gettext_noop("Destinations")
GROUP_ACTIONS: Final = gettext_noop("Actions")
GROUP_PATIENTS: Final = gettext_noop("Patients")
GROUP_VIEWS: Final = gettext_noop("Saved views")
RUN_URL_NAME: Final = "workspace-command-run"


class CommandQueryError(ValueError):
    """Reject a query outside the accepted length without reflecting it."""

    def __init__(self) -> None:
        """Expose one payload-free message."""
        super().__init__("command query is invalid")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One palette row. ``subject`` stays server-side behind a session token."""

    kind: str
    group: str
    label: str
    meta: str
    action_url_name: str
    href: str | None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class CommandSearch:
    """The ordered rows plus the patient-matching outcome."""

    results: tuple[CommandResult, ...]
    patient_status: str = "not_searched"


def _place_result(
    place: Destination, kind: str, group: str, clinic_id: UUID
) -> CommandResult:
    if place.url_name is None:  # open_places never yields one; keep mypy honest
        msg = "undelivered destination"
        raise ValueError(msg)
    return CommandResult(
        kind=kind,
        group=gettext(group),
        label=place.label,
        meta="",
        action_url_name=place.url_name,
        href=destination_url(place, clinic_id),
    )


def _matches(label: str, needle: str) -> bool:
    return not needle or needle in normalized(label)


def _patient_matches(
    *, clinic_id: UUID, timezone: str, query: str
) -> tuple[tuple[CommandResult, ...], str]:
    """Return exact full-name matches from the audited registry search."""
    needle = normalized(query)
    today = date.fromisoformat(clinic_local_today(timezone))
    found: list[CommandResult] = []
    try:
        page_number, page_count = 1, 1
        while page_number <= page_count:
            if page_number > MAX_PATIENT_PAGES:
                return (), "refine"
            page = search_patients(clinic_id=clinic_id, query=query, page=page_number)
            page_count = page.page_count
            found.extend(
                CommandResult(
                    kind="patient",
                    group=gettext(GROUP_PATIENTS),
                    label=item.full_name,
                    meta=age_label(item.birth_date, today),
                    action_url_name=RUN_URL_NAME,
                    href=None,
                    subject=str(item.enrollment_id),
                )
                for item in page.items
                if normalized(item.full_name) == needle
            )
            page_number += 1
    except PatientSearchInputError:
        return (), "no_match"
    except PatientAccessDeniedError:
        return (), "not_searched"
    if len(found) > MAX_PATIENT_MATCHES:
        return (), "refine"
    return tuple(found), "matched" if found else "no_match"


def _view_results(  # noqa: PLR0913 - the saved-view rows need the shell scope
    *,
    clinic_id: UUID,
    timezone: str,
    roles: frozenset[str],
    granted: frozenset[str],
    needle: str,
    context_path: str,
) -> list[CommandResult]:
    """Return saved views to open or remove, and 'save this view' for the page."""
    group = gettext(GROUP_VIEWS)
    rows: list[CommandResult] = []
    saved = set()
    for record in list_saved_views(clinic_id=clinic_id):
        spec = ViewSpec(record.destination, record.params)
        saved.add(spec.subject())
        if not is_supported(spec) or not destination_open(
            spec, roles=roles, granted=granted
        ):
            continue
        label = view_label(spec)
        if not _matches(label, needle):
            continue
        rows.append(
            CommandResult(
                kind="saved_view",
                group=group,
                label=label,
                meta="",
                action_url_name="scheduling:agenda-at",
                href=view_href(record, clinic_id=clinic_id, timezone=timezone),
            )
        )
        rows.append(
            CommandResult(
                kind="archive_view",
                group=group,
                label=gettext("Remove saved view: %(name)s") % {"name": label},
                meta="",
                action_url_name=RUN_URL_NAME,
                href=None,
                subject=str(record.id),
            )
        )
    current = spec_from_path(context_path, clinic_id) if context_path else None
    if (
        current is not None
        and current.subject() not in saved
        and destination_open(current, roles=roles, granted=granted)
    ):
        label = gettext("Save this view: %(name)s") % {"name": view_label(current)}
        if _matches(label, needle):
            rows.append(
                CommandResult(
                    kind="save_view",
                    group=group,
                    label=label,
                    meta="",
                    action_url_name=RUN_URL_NAME,
                    href=None,
                    subject=current.subject(),
                )
            )
    return rows


def search_commands(
    *,
    clinic_id: UUID,
    timezone: str,
    roles: frozenset[str],
    query: str,
    context_path: str = "",
) -> CommandSearch:
    """Search the actor's allowed commands in one clinic they hold a role in.

    ``roles`` are the actor's roles in that clinic as the shell resolved
    them; bundle permissions are re-read from the database here.
    ``context_path`` is the page the palette was opened on (it may offer
    "save this view"); it never selects a record.
    """
    if not isinstance(query, str) or len(query) > MAX_COMMAND_QUERY:
        raise CommandQueryError
    granted = granted_permissions(clinic_id)
    needle = normalized(query)
    results: list[CommandResult] = [
        _place_result(place, "destination", GROUP_DESTINATIONS, clinic_id)
        for place in open_places(DESTINATIONS, roles=roles, granted=granted)
        if _matches(place.label, needle)
    ]
    results.extend(
        _place_result(place, "action", GROUP_ACTIONS, clinic_id)
        for place in open_places(ACTIONS, roles=roles, granted=granted)
        if _matches(place.label, needle)
    )
    results.extend(
        _view_results(
            clinic_id=clinic_id,
            timezone=timezone,
            roles=roles,
            granted=granted,
            needle=needle,
            context_path=context_path,
        )
    )
    patient_status = "not_searched"
    if (
        DEMOGRAPHICS_READ in granted
        and roles & MANAGER_ROUTE
        and len(needle.split()) >= MIN_NAME_WORDS
    ):
        patients, patient_status = _patient_matches(
            clinic_id=clinic_id, timezone=timezone, query=query
        )
        results.extend(patients)
    return CommandSearch(results=tuple(results), patient_status=patient_status)
