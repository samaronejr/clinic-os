"""Current clinic context and role-aware navigation for the shared shell.

The navigation only reflects authorization; every module route keeps its own
server-side gate. Clinic visibility is bounded by row-level security on the
active tenant, so a clinic from another organization can never be resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.contrib.auth import SESSION_KEY
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from apps.identity.current_context import MANAGER_ROLES
from apps.identity.models import User, UserClinicRole
from apps.scheduling.agenda_presenter import clinic_local_today

if TYPE_CHECKING:
    from django.http import HttpRequest

ACTIVE_CLINIC_SESSION_KEY: Final = "active_clinic_id"
AUTH_FLOW_VIEWS: Final = frozenset(
    {
        "identity:login",
        "identity:logout",
        "identity:enroll",
        "identity:verify",
        "identity:step-up",
    }
)
_STAFF_ROLES: Final = (*MANAGER_ROLES, UserClinicRole.Role.PHYSICIAN)
_VIEW_SECTIONS: Final = {
    "scheduling:agenda": "agenda",
    "scheduling:agenda-at": "agenda",
    "scheduling:appointment-create": "agenda",
    "scheduling:appointment-reschedule": "agenda",
    "scheduling:appointment-cancel": "agenda",
    "intake:patient-list": "patients",
    "intake:patient-create": "patients",
    "intake:patient-contacts": "patients",
    "intake:patient-access": "patients",
    "scheduling:availability-list": "availability",
    "scheduling:availability-retire": "availability",
    "retention:status": "retention",
    "consent:staff": "consent",
    "billing:charges": "billing",
    "billing:invoice": "billing",
    "identity:clinic-settings": "settings",
}


@dataclass(frozen=True, slots=True)
class WorkspaceClinic:
    """One clinic the current user holds at least one role in."""

    id: UUID
    name: str
    timezone: str
    roles: frozenset[str]

    @property
    def agenda_url(self) -> str:
        """Return today's agenda for this clinic."""
        return reverse("scheduling:agenda", args=(self.id,))


@dataclass(frozen=True, slots=True)
class WorkspaceLink:
    """One working module the current role may open."""

    key: str
    label: str
    url: str
    current: bool
    badge: str = ""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Everything the shell needs to orient an authenticated user."""

    username: str
    clinic: WorkspaceClinic | None
    other_clinics: tuple[WorkspaceClinic, ...]
    links: tuple[WorkspaceLink, ...]

    @property
    def home_url(self) -> str:
        """Return the brand link target: today's agenda or the workspace home."""
        if self.clinic is None:
            return reverse("workspace-home")
        return self.clinic.agenda_url


def is_auth_flow(request: HttpRequest) -> bool:
    """Report whether the request renders one of the focused authentication screens."""
    match = request.resolver_match
    return match is not None and match.view_name in AUTH_FLOW_VIEWS


def has_session_identity(request: HttpRequest) -> bool:
    """Report whether the session carries both halves of a tenant identity."""
    session = request.session
    return isinstance(session.get(SESSION_KEY), str) and isinstance(
        session.get("active_org_id"), str
    )


def _parse_uuid(raw_value: object) -> UUID | None:
    if isinstance(raw_value, UUID):
        return raw_value
    if not isinstance(raw_value, str):
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _visible_clinics(user_id: UUID) -> tuple[WorkspaceClinic, ...]:
    grouped: dict[UUID, tuple[str, str, set[str]]] = {}
    assignments = (
        UserClinicRole.objects.filter(user_id=user_id, role__in=_STAFF_ROLES)
        .select_related("clinic")
        .order_by("clinic__name", "clinic_id")
    )
    for assignment in assignments:
        clinic = assignment.clinic
        # The database rejects blank zones; str() only narrows the field type.
        zone = str(clinic.timezone)
        entry = grouped.setdefault(clinic.pk, (clinic.name, zone, set()))
        entry[2].add(assignment.role)
    return tuple(
        WorkspaceClinic(id=clinic_id, name=name, timezone=zone, roles=frozenset(roles))
        for clinic_id, (name, zone, roles) in grouped.items()
    )


def _select_clinic(
    request: HttpRequest,
    clinics: tuple[WorkspaceClinic, ...],
) -> WorkspaceClinic | None:
    by_id = {clinic.id: clinic for clinic in clinics}
    match = request.resolver_match
    requested = _parse_uuid(match.kwargs.get("clinic_id")) if match else None
    remembered = _parse_uuid(request.session.get(ACTIVE_CLINIC_SESSION_KEY))
    selected = by_id.get(requested) if requested else None
    if selected is None and remembered is not None:
        selected = by_id.get(remembered)
    if selected is None and clinics:
        selected = clinics[0]
    persisted = str(selected.id) if selected else None
    if persisted != request.session.get(ACTIVE_CLINIC_SESSION_KEY):
        if persisted is None:
            request.session.pop(ACTIVE_CLINIC_SESSION_KEY, None)
        else:
            request.session[ACTIVE_CLINIC_SESSION_KEY] = persisted
    return selected


def _agenda_badge(clinic: WorkspaceClinic) -> str:
    today = date.fromisoformat(clinic_local_today(clinic.timezone))
    return str(_("today %(day)s") % {"day": date_format(today, "d/m")})


def _links(request: HttpRequest, clinic: WorkspaceClinic) -> tuple[WorkspaceLink, ...]:
    match = request.resolver_match
    section = _VIEW_SECTIONS.get(match.view_name, "") if match else ""
    manager = bool(clinic.roles.intersection(MANAGER_ROLES))
    links = [
        WorkspaceLink(
            key="agenda",
            label=str(_("Agenda")),
            url=clinic.agenda_url,
            current=section == "agenda",
            badge=_agenda_badge(clinic),
        )
    ]
    if clinic.roles.intersection(
        {UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN}
    ):
        links.append(
            WorkspaceLink(
                key="settings",
                label="Configurações",
                url=reverse("identity:clinic-settings", args=(clinic.id,)),
                current=section == "settings",
            )
        )
    if manager:
        links.append(
            WorkspaceLink(
                key="patients",
                label=str(_("Patients")),
                url=reverse("intake:patient-list", args=(clinic.id,)),
                current=section == "patients",
            )
        )
        links.append(
            WorkspaceLink(
                key="billing",
                label="Cobranças",
                url=reverse("billing:charges", args=(clinic.id,)),
                current=section == "billing",
            )
        )
    links.append(
        WorkspaceLink(
            key="availability",
            label=str(_("Availability")),
            url=reverse("scheduling:availability-list", args=(clinic.id,)),
            current=section == "availability",
        )
    )
    if manager or UserClinicRole.Role.PHYSICIAN in clinic.roles:
        links.append(
            WorkspaceLink(
                key="retention",
                label=str(_("Retention")),
                url=reverse("retention:status", args=(clinic.id,)),
                current=section == "retention",
            )
        )
    links.append(
        WorkspaceLink(
            key="consent",
            label="Consentimentos",
            url=reverse("consent:staff", args=(clinic.id,)),
            current=section == "consent",
        )
    )
    return tuple(links)


def resolve_workspace(request: HttpRequest) -> Workspace | None:
    """Resolve the shell context for an authenticated request, or ``None``.

    A request refused before URL resolution (the tenant boundary's 403) has no
    trusted identity to read, so the shell stays brand-only for it.
    """
    if request.resolver_match is None:
        return None
    user = request.user
    if not user.is_authenticated or not isinstance(user, User):
        return None
    clinics = _visible_clinics(user.pk)
    clinic = _select_clinic(request, clinics)
    return Workspace(
        username=user.get_username(),
        clinic=clinic,
        other_clinics=tuple(entry for entry in clinics if entry is not clinic),
        links=_links(request, clinic) if clinic else (),
    )
