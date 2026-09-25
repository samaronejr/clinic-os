"""Current clinic context, navigation and patient banner for the shared shell.

The destinations come from the ``apps.core.navigation`` registry, filtered by
the actor's permission bundle and each route's own guard; every module route
keeps its own server-side gate. Clinic visibility is bounded by row-level
security on the active tenant, so a clinic from another organization can
never be resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.contrib.auth import SESSION_KEY
from django.urls import reverse

from apps.core.navigation import (
    NavLink,
    PaletteGroup,
    build_navigation,
    granted_permissions,
)
from apps.core.patient_context import PatientBanner, resolve_patient_banner
from apps.identity.current_context import MANAGER_ROLES
from apps.identity.models import User, UserClinicRole

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
class Workspace:
    """Everything the shell needs to orient an authenticated user."""

    username: str
    clinic: WorkspaceClinic | None
    other_clinics: tuple[WorkspaceClinic, ...]
    links: tuple[NavLink, ...]
    section: tuple[NavLink, ...] = ()
    section_label: str = ""
    section_current: str = ""
    palette_groups: tuple[PaletteGroup, ...] = ()
    patient: PatientBanner | None = None
    resume_path: str = ""

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


def _targets_clinic(request: HttpRequest, clinic: WorkspaceClinic) -> bool:
    """Report whether the request names no clinic or the shell's own clinic.

    A request for another clinic (refused, so the shell fell back to the
    remembered clinic) shows neither that clinic nor the pinned patient.
    """
    match = request.resolver_match
    requested = match.kwargs.get("clinic_id") if match else None
    return requested is None or _parse_uuid(requested) == clinic.id


def _resume_path(request: HttpRequest, clinic: WorkspaceClinic) -> str:
    """Return the page to come back to after a palette switch.

    Only a path inside the clinic in context is echoed back; a refused page
    for another clinic resumes at the workspace home, so no response ever
    repeats a foreign clinic's identifier.
    """
    if not _targets_clinic(request, clinic):
        return reverse("workspace-home")
    return request.path


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
    others = tuple(entry for entry in clinics if entry is not clinic)
    if clinic is None:
        return Workspace(user.get_username(), None, others, ())
    view_name = request.resolver_match.view_name
    granted = granted_permissions(clinic.id)
    navigation = build_navigation(
        clinic_id=clinic.id,
        timezone=clinic.timezone,
        roles=clinic.roles,
        granted=granted,
        view_name=view_name,
    )
    return Workspace(
        username=user.get_username(),
        clinic=clinic,
        other_clinics=others,
        links=navigation.links,
        section=navigation.section,
        section_label=navigation.section_label,
        section_current=navigation.section_current,
        palette_groups=navigation.palette,
        resume_path=_resume_path(request, clinic),
        patient=(
            resolve_patient_banner(
                request,
                clinic_id=clinic.id,
                timezone=clinic.timezone,
                roles=clinic.roles,
                granted=granted,
                view_name=view_name,
            )
            if _targets_clinic(request, clinic)
            else None
        ),
    )


def current_clinic(request: HttpRequest) -> WorkspaceClinic | None:
    """Return the shell's clinic for a request whose path names no clinic.

    The palette endpoints use the clinic the shell last showed (the session's
    remembered clinic when the actor still holds a role there), resolved by
    the same membership query as the shell itself.
    """
    user = request.user
    if (
        request.resolver_match is None
        or not user.is_authenticated
        or not isinstance(user, User)
    ):
        return None
    return _select_clinic(request, _visible_clinics(user.pk))


def clinic_of_actor(user_id: UUID, clinic_id: UUID) -> WorkspaceClinic | None:
    """Return one clinic the actor holds a shell role in, else ``None``.

    Unknown, foreign-organization and unassigned clinics are indistinguishable.
    """
    return next(
        (clinic for clinic in _visible_clinics(user_id) if clinic.id == clinic_id),
        None,
    )
