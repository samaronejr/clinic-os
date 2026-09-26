"""The staff-state matrix every exemption probe executes under.

The census's primary rule is that an exempt function never observes the
actor at all (identity/actor_channels.py): that holds for every staff state,
enumerated or not. This matrix is the behavioural cross-check (defence in
depth). An exemption claims that a function's outcome does not depend on staff
permission state. The states varied here come from the inputs the live
permission decision reads (identity/permission_inputs.py). Every derived
input is declared in ``DIMENSIONS``: either varied, with the full value set
the matrix realizes (checked against the rows actually written), or held
fixed, with the reason it cannot change a decision. A new input that is not
declared fails ``test_probe_matrix_covers_every_decision_input``.

Families (all committed before the first probe runs unless noted):
- ``power``: every subset of the role vocabulary at the probed clinic
  (2**10 = 1024), professional roles unregistered.
- ``credential``: every combination of the bundle classes (roles with an
  identical bundle form one class, taken from the live-verified
  ``BUNDLES_V1``), with each professional class at one of bare,
  registered or care-scoped, where at least one professional class is
  registered or scoped (the rest are in ``power``).
- ``realization``: per professional role, every registration, care-team
  and (physician) legacy-profile value one at a time on top of an
  otherwise valid credential.
- ``inactive``, ``elsewhere``: an inactive user (bare and fully
  credentialed), roles only in another clinic or organization.
- ``flags``: a receptionist with each other boolean user flag set
  (is_staff, is_superuser; derived from the User model).
- ``assigned``: the physician of the probed open encounter; a physician
  whose only encounter was closed through the real finalization path; then
  (phase) the first physician registered, so the open-encounter branch grants.
- ``removal``: one state per (role, permission) pair with exactly that pair
  removed, for an actor holding only that role (credentialed when the role
  is professional). Removals are clinic-wide, so each is applied inside the
  state's scopes and rolled back with them: linear, not a power set.
- ``grant`` (phase, last): ineffective role-grant removals (expired, future,
  other clinic), then current removals of every permission, one role at a
  time and cumulative, for the fully credentialed actor.
- ``control`` (last): the ``none`` actor again, after every phase.
Phase states mutate shared rows, so they run after every committed state
and in this order; the final control shows a phase did not move the
outcome for an actor it does not touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations, product
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from apps.identity.models import (
    CareTeamMembership,
    PhysicianProfile,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from apps.identity.permissions import BUNDLES_V1, PROFESSIONAL_PERMISSIONS_V1
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import connection
from django.utils import timezone
from psycopg import sql

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from django.db.backends.utils import CursorWrapper

    from rbac_fixtures import RbacGraph

ROLES: Final = tuple(sorted(UserClinicRole.Role.values))
PROFESSIONAL_ROLES: Final = tuple(
    role for role in ROLES if BUNDLES_V1[role] & PROFESSIONAL_PERMISSIONS_V1
)
STATUSES: Final = tuple(PhysicianProfile.Status.values)
_COUNCIL: Final = {"physician": "CRM", "nurse": "COREN", "allied_professional": "CRP"}
REGISTRATION_VARIANTS: Final = (
    "revoked",
    "expired",
    "future",
    "jurisdiction",
    "other_clinic",
    "other_role",
    *(f"status:{status}" for status in STATUSES if status != "regular"),
)
CARE_VARIANTS: Final = (
    "open",
    "bounded",
    "revoked",
    "expired",
    "future",
    "other_role",
    "other_enrollment",
    "other_clinic",
)
PROFILE_VARIANTS: Final = (
    "valid",
    "valid_expiring",
    "expired",
    "stale",
    "unchecked",
    "checked_in_future",
    "no_recheck",
    "unsynthetic",
    "other_jurisdiction",
    "other_user",
    *(f"status:{status}" for status in STATUSES if status != "regular"),
)
LEVELS: Final = ("bare", "registered", "scoped")
# Boolean identity_user columns other than is_active, which the matrix
# varies on its own (the loader hands the whole row to Python callers).
USER_FLAGS: Final = tuple(
    sorted(
        field.name
        for field in User._meta.concrete_fields
        if field.get_internal_type() == "BooleanField" and field.name != "is_active"
    )
)


def role_label(roles: Iterable[str]) -> str:
    """``none``, the role itself, ``all``, or ``roles:a+b``."""
    chosen = sorted(roles)
    if not chosen:
        return "none"
    if len(chosen) == 1:
        return chosen[0]
    if tuple(chosen) == ROLES:
        return "all"
    return "roles:" + "+".join(chosen)


def bundle_classes() -> tuple[tuple[str, ...], ...]:
    """Roles grouped by identical permission bundle, sorted."""
    classes: dict[frozenset[str], list[str]] = {}
    for role in ROLES:
        classes.setdefault(BUNDLES_V1[role], []).append(role)
    return tuple(sorted(tuple(sorted(group)) for group in classes.values()))


@dataclass(frozen=True, slots=True)
class Credential:
    """A professional role's evidence: registration, care scope, profile."""

    registration: str | None = None
    care: str | None = None
    profile: str | None = None


_LEVEL: Final = {
    "bare": Credential(),
    "registered": Credential("current"),
    "scoped": Credential("current", "open"),
}


@dataclass(frozen=True, slots=True)
class ActorSpec:
    """One staff state: roles here, credentials, activity, other memberships."""

    label: str
    family: str
    roles: tuple[str, ...]
    credentials: tuple[tuple[str, Credential], ...] = ()
    active: bool = True
    elsewhere: tuple[str, ...] = ("clinic_b:receptionist",)
    flags: tuple[str, ...] = ()


def actor_specs() -> tuple[ActorSpec, ...]:
    """Every committed state, in execution order."""
    specs: list[ActorSpec] = [
        ActorSpec(role_label(roles), "power", roles)
        for size in range(len(ROLES) + 1)
        for roles in combinations(ROLES, size)
    ]
    specs.extend(_credential_specs())
    for role in PROFESSIONAL_ROLES:
        variants = [
            ("registered", Credential("current")),
            *((f"care:{v}", Credential("current", v)) for v in CARE_VARIANTS),
            *(
                (f"registration:{v}", Credential(v, "open"))
                for v in REGISTRATION_VARIANTS
            ),
        ]
        if role == "physician":
            variants.extend(
                (f"profile:{v}", Credential("current", "open", v))
                for v in PROFILE_VARIANTS
            )
        specs.extend(
            ActorSpec(f"{role}@{name}", "realization", (role,), ((role, cred),))
            for name, cred in variants
        )
    maximal = tuple((role, _LEVEL["scoped"]) for role in PROFESSIONAL_ROLES)
    specs.extend(
        (
            ActorSpec("maximal", "credential", ROLES, maximal),
            ActorSpec("inactive:none", "inactive", (), (), active=False),
            ActorSpec("inactive:maximal", "inactive", ROLES, maximal, active=False),
            ActorSpec(
                "elsewhere:other_clinic",
                "elsewhere",
                (),
                (),
                elsewhere=tuple(f"clinic_b:{role}" for role in ROLES),
            ),
            ActorSpec(
                "elsewhere:other_organization",
                "elsewhere",
                (),
                (),
                elsewhere=(
                    "clinic_b:receptionist",
                    *(f"clinic_c:{role}" for role in ROLES),
                ),
            ),
            *(
                ActorSpec(f"flag:{flag}", "flags", ("receptionist",), flags=(flag,))
                for flag in USER_FLAGS
            ),
        )
    )
    return tuple(specs)


def _credential_specs() -> list[ActorSpec]:
    classes = bundle_classes()
    professional = [group for group in classes if set(group) & set(PROFESSIONAL_ROLES)]
    plain = [group for group in classes if group not in professional]
    specs: list[ActorSpec] = []
    for present in product((False, True), repeat=len(plain)):
        for levels in product((None, *LEVELS), repeat=len(professional)):
            if not {"registered", "scoped"} & set(levels):
                continue
            roles = [group[0] for group, on in zip(plain, present, strict=True) if on]
            credentials: list[tuple[str, Credential]] = []
            parts = list(roles)
            for group, level in zip(professional, levels, strict=True):
                if level is None:
                    continue
                roles.append(group[0])
                credentials.append((group[0], _LEVEL[level]))
                parts.append(f"{group[0]}@{level}")
            specs.append(
                ActorSpec(
                    "credential:" + "+".join(sorted(parts)),
                    "credential",
                    tuple(sorted(roles)),
                    tuple(credentials),
                )
            )
    return specs


@dataclass(frozen=True, slots=True)
class ProbeState:
    """An executed state: its actor and an optional shared-row phase setup."""

    label: str
    family: str
    actor: UUID
    setup: Callable[[], None] | None = None
    scoped: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class _Places:
    graph: RbacGraph
    enrollments: tuple[UUID, ...]
    other_enrollment: UUID
    other_clinic_enrollment: UUID


def _enrollment(graph: RbacGraph, clinic: UUID) -> UUID:
    patient = Patient.objects.create(
        organization_id=graph.organization_a,
        full_name="Sintetico Matriz",
        birth_date=date(1990, 1, 1),
    )
    return PatientClinicEnrollment.objects.create(
        organization_id=graph.organization_a,
        clinic_id=clinic,
        patient=patient,
        idempotency_key=uuid4(),
        create_fingerprint=b"s" * 32,
    ).pk


def _window(kind: str, now: datetime) -> tuple[datetime, datetime | None]:
    day = timedelta(days=1)
    return {
        "open": (now - day, None),
        "current": (now - day, now + 30 * day),
        "bounded": (now - day, now + 30 * day),
        "expired": (now - 3 * day, now - 2 * day),
        "future": (now + 2 * day, now + 3 * day),
    }[kind]


def _other_professional(role: str) -> str:
    return next(other for other in PROFESSIONAL_ROLES if other != role)


def _registration(  # noqa: PLR0913 - one row, every varied column
    places: _Places,
    user: UUID,
    role: str,
    variant: str,
    now: datetime,
    profile: UUID | None = None,
) -> ProfessionalRegistration:
    graph = places.graph
    registered = _other_professional(role) if variant == "other_role" else role
    clinic = graph.clinic_b if variant == "other_clinic" else graph.clinic_a
    added = _member(graph, user, clinic, registered)
    window = (
        "expired"
        if variant == "expired"
        else "future"
        if variant == "future"
        else "current"
    )
    valid_from, valid_to = _window(window, now)
    row = ProfessionalRegistration.objects.create(
        organization_id=graph.organization_a,
        clinic_id=clinic,
        user_id=user,
        role=registered,
        council=_COUNCIL[registered],
        number=f"SINTETICO-{uuid4().hex[:8]}",
        jurisdiction="RJ" if variant == "jurisdiction" else "SP",
        specialty="Sintetico",
        status=variant.split(":", 1)[1] if variant.startswith("status:") else "regular",
        valid_from=valid_from,
        valid_to=valid_to or now + timedelta(days=30),
        revoked_at=now - timedelta(hours=1) if variant == "revoked" else None,
        physician_profile_id=profile,
    )
    if added and variant == "other_role":
        # Registered for a role the actor no longer holds.
        UserClinicRole.objects.filter(
            clinic_id=clinic, user_id=user, role=registered
        ).delete()
    return row


def _profile(graph: RbacGraph, user: UUID, variant: str, now: datetime) -> UUID:
    """A legacy profile the insert guard accepts (same user and UF)."""
    day = timedelta(days=1)
    status = variant.split(":", 1)[1] if variant.startswith("status:") else "regular"
    return PhysicianProfile.objects.create(
        organization_id=graph.organization_a,
        user_id=user,
        jurisdiction="SP",
        registration_number=f"SINTETICO-{uuid4().hex[:12]}",
        signing_subject="Sintetico",
        synthetic=variant != "unsynthetic",
        status=status,
        expires_at={
            "valid_expiring": now + 30 * day,
            "expired": now - day,
        }.get(variant),
        last_checked_at={
            "unchecked": None,
            "checked_in_future": now + day,
        }.get(variant, now - 2 * day),
        recheck_at={"stale": now - day, "no_recheck": None}.get(
            variant, now + 30 * day
        ),
    ).pk


def _relink(profile: UUID, variant: str) -> None:
    """Move a linked profile away after the guard checked the link.

    identity_scope_guard checks the link only on insert and the profile
    table has no guard, so a later owner update reaches these values.
    """
    if variant == "other_user":
        other = User.objects.create(username=f"probe-profile-{uuid4().hex}").pk
        PhysicianProfile.objects.filter(pk=profile).update(user_id=other)
    elif variant == "other_jurisdiction":
        PhysicianProfile.objects.filter(pk=profile).update(jurisdiction="RJ")


def _member(graph: RbacGraph, user: UUID, clinic: UUID, role: str) -> bool:
    """Ensure the canonical membership the insert guard requires."""
    _, created = UserClinicRole.objects.get_or_create(
        organization_id=graph.organization_a, clinic_id=clinic, user_id=user, role=role
    )
    return created


def _care(places: _Places, user: UUID, role: str, variant: str, now: datetime) -> None:
    graph = places.graph
    window = variant if variant in ("open", "bounded", "expired", "future") else "open"
    valid_from, valid_to = _window(window, now)
    if variant == "other_enrollment":
        enrollments: tuple[UUID, ...] = (places.other_enrollment,)
    elif variant == "other_clinic":
        enrollments = (places.other_clinic_enrollment,)
    else:
        enrollments = places.enrollments
    clinic = graph.clinic_b if variant == "other_clinic" else graph.clinic_a
    scoped = _other_professional(role) if variant == "other_role" else role
    added = _member(graph, user, clinic, scoped)
    for enrollment in enrollments:
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient_enrollment_id=enrollment,
            user_id=user,
            role=scoped,
            valid_from=valid_from,
            valid_to=valid_to,
            revoked_at=now - timedelta(hours=1) if variant == "revoked" else None,
        )
    if added and variant == "other_role":
        # Scoped in a role the actor no longer holds.
        UserClinicRole.objects.filter(
            clinic_id=clinic, user_id=user, role=scoped
        ).delete()


def _credential(
    places: _Places, user: UUID, role: str, credential: Credential, now: datetime
) -> None:
    if credential.registration is not None:
        profile = (
            None
            if credential.profile is None
            else _profile(places.graph, user, credential.profile, now)
        )
        _registration(places, user, role, credential.registration, now, profile)
        if profile is not None and credential.profile is not None:
            _relink(profile, credential.profile)
    if credential.care is not None:
        _care(places, user, role, credential.care, now)


@dataclass(frozen=True, slots=True)
class Matrix:
    """The ordered states and the places their rows point at."""

    states: tuple[ProbeState, ...]
    places: _Places
    applied: set[str] = field(default_factory=set)


def build_states(graph: RbacGraph, assigned: UUID, closed: UUID) -> Matrix:
    """Write every state's rows (committed) and return the ordered states.

    ``assigned`` is the physician of the probed open encounter; ``closed``
    a physician whose only encounter was closed through the real
    finalization path (exemption_probes.closed_encounter_physician).
    """
    now = timezone.now()
    specs = actor_specs()
    users = User.objects.bulk_create(
        [
            User(username=f"probe-{index}-{uuid4().hex[:12]}")
            for index, spec in enumerate(specs)
        ]
    )
    clinics = {
        "clinic_a": (graph.organization_a, graph.clinic_a),
        "clinic_b": (graph.organization_a, graph.clinic_b),
        "clinic_c": (graph.organization_b, graph.clinic_c),
    }
    with owner_context(graph.organization_a):
        places = _Places(
            graph,
            tuple(
                PatientClinicEnrollment.objects.filter(
                    clinic_id=graph.clinic_a
                ).values_list("pk", flat=True)
            ),
            _enrollment(graph, graph.clinic_a),
            _enrollment(graph, graph.clinic_b),
        )
    rows: dict[UUID, list[UserClinicRole]] = {}
    for spec, user in zip(specs, users, strict=True):
        placed = [f"clinic_a:{role}" for role in spec.roles] + list(spec.elsewhere)
        for item in placed:
            clinic_key, role = item.split(":", 1)
            organization, clinic = clinics[clinic_key]
            rows.setdefault(organization, []).append(
                UserClinicRole(
                    organization_id=organization,
                    clinic_id=clinic,
                    user_id=user.pk,
                    role=role,
                )
            )
    for organization, batch in rows.items():
        with owner_context(organization):
            UserClinicRole.objects.bulk_create(batch)
    with owner_context(graph.organization_a):
        for spec, user in zip(specs, users, strict=True):
            for role, credential in spec.credentials:
                _credential(places, user.pk, role, credential, now)
    # The scope guard needs an active member at insert; deactivate after.
    User.objects.filter(
        pk__in=[
            user.pk for spec, user in zip(specs, users, strict=True) if not spec.active
        ]
    ).update(is_active=False)
    for flag in USER_FLAGS:
        User.objects.filter(
            pk__in=[
                user.pk
                for spec, user in zip(specs, users, strict=True)
                if flag in spec.flags
            ]
        ).update(**{flag: True})
    states = [
        ProbeState(spec.label, spec.family, user.pk)
        for spec, user in zip(specs, users, strict=True)
    ]
    by_label = {state.label: state.actor for state in states}
    states.append(ProbeState("assigned", "assigned", assigned))
    states.append(ProbeState("assigned:closed", "assigned", closed))
    states.extend(_pair_removals(graph, by_label, now))
    states.extend(_phases(places, by_label, assigned, now))
    return Matrix(tuple(states), places)


def _pair_removals(
    graph: RbacGraph, actors: Mapping[str, UUID], now: datetime
) -> list[ProbeState]:
    """One state per (role, permission): exactly that pair removed, for an
    actor holding only that role (credentialed and care-scoped when the
    role is professional, so every bundle permission is live).

    Removals are clinic-wide, so each is applied inside the state's scopes
    and rolled back with them (``ProbeState.scoped``).
    """

    def remove(role: str, permission: str) -> Callable[[], None]:
        def apply() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                row = cursor.fetchone()
                assert row is not None
                cursor.execute("SET LOCAL ROLE clinic_owner")
                RoleGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    role=role,
                    permission=permission,
                    valid_from=now - timedelta(days=1),
                )
                cursor.execute(
                    sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(str(row[0])))
                )

        return apply

    return [
        ProbeState(
            f"removed:{role}:{permission}",
            "removal",
            actors[f"{role}@care:open" if role in PROFESSIONAL_ROLES else role],
            scoped=remove(role, permission),
        )
        for role in ROLES
        for permission in sorted(BUNDLES_V1[role])
    ]


def _phases(
    places: _Places, actors: Mapping[str, UUID], assigned: UUID, now: datetime
) -> list[ProbeState]:
    graph = places.graph

    def register_assigned() -> None:
        with owner_context(graph.organization_a):
            _registration(places, assigned, "physician", "current", now)

    def ineffective_grants() -> None:
        with owner_context(graph.organization_a):
            RoleGrant.objects.bulk_create(
                RoleGrant(
                    organization_id=graph.organization_a,
                    clinic_id=clinic,
                    role=role,
                    permission=permission,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
                for role in ROLES
                for permission in sorted(BUNDLES_V1[role])
                for clinic, (valid_from, valid_to) in (
                    (graph.clinic_a, _window("expired", now)),
                    (graph.clinic_a, _window("future", now)),
                    (graph.clinic_b, _window("open", now)),
                )
            )

    def remove(role: str) -> Callable[[], None]:
        # Alternate open-ended and bounded current windows.
        valid_from, valid_to = _window(
            "open" if ROLES.index(role) % 2 else "current", now
        )

        def apply() -> None:
            with owner_context(graph.organization_a):
                RoleGrant.objects.bulk_create(
                    RoleGrant(
                        organization_id=graph.organization_a,
                        clinic_id=graph.clinic_a,
                        role=role,
                        permission=permission,
                        valid_from=valid_from,
                        valid_to=valid_to,
                    )
                    for permission in sorted(BUNDLES_V1[role])
                )

        return apply

    return [
        ProbeState("assigned:registered", "assigned", assigned, register_assigned),
        ProbeState("grant:ineffective", "grant", actors["maximal"], ineffective_grants),
        *(
            ProbeState(
                f"grant:removed:{role}", "grant", actors["maximal"], remove(role)
            )
            for role in ROLES
        ),
        ProbeState("control:none", "control", actors["none"]),
    ]


# Every input the live decision reads (permission_inputs.decision_inputs),
# with how the matrix covers it: ("varied", values) are realized states and
# are checked against the written rows by ``realized``; ("fixed", reason)
# explains why the input cannot distinguish two staff states of one call.
_KEY = "join key: fixed by the row the decision is asked about"
_CLINIC = (
    "the probed clinic is a call input; its crm_uf is compared with the "
    "registration jurisdiction, which is varied"
)
_ENROLLMENT = (
    "call input check: the enrollment argument must belong to the clinic; "
    "chosen by the probed code, not by staff state"
)
_USER_ROW = (
    "read only through load_current_user(), which hands the whole row to its "
    "Python callers; an exempt function never reaches it, because loading the "
    "actor is an actor observation the census refuses (identity/"
    "actor_channels.py). Boolean flags are also varied as states."
)
_WINDOW = frozenset({"open", "current", "expired", "future"})
_BOUNDED = frozenset({"current", "expired", "future"})
_PLACE = frozenset({"this_clinic", "other_clinic"})
_MOMENT = frozenset({"null", "past", "future"})
DIMENSIONS: Final[Mapping[str, tuple[str, frozenset[str] | str]]] = {
    "setting:has_permission:app.current_user_id": (
        "fixed",
        "each state binds its own actor; that actor's rows are the varied inputs",
    ),
    "setting:has_permission:app.current_tenant": (
        "fixed",
        "the probe contexts bind the world organization; membership in another "
        "organization is varied through identity_userclinicrole.organization_id",
    ),
    "call:has_permission:statement_timestamp": (
        "fixed",
        "the clock; every window column is varied relative to it",
    ),
    "call:has_permission:timestamptz": ("fixed", "a type name in DECLARE, not a read"),
    "param:has_permission.perm": ("fixed", "call input chosen by the probed code"),
    "param:has_permission.clinic": ("fixed", "call input chosen by the probed code"),
    "param:has_permission.enrollment": (
        "fixed",
        "call input chosen by the probed code",
    ),
    "column:has_permission:identity_user.id": ("fixed", _KEY),
    "column:has_permission:identity_user.is_active": (
        "varied",
        frozenset({"true", "false"}),
    ),
    "column:load_current_user:identity_user.is_active": (
        "varied",
        frozenset({"true", "false"}),
    ),
    "column:load_current_user:identity_user.id": ("fixed", _KEY),
    **{
        f"column:load_current_user:identity_user.{flag}": (
            "varied",
            frozenset({"true", "false"}),
        )
        for flag in USER_FLAGS
    },
    "setting:load_current_user:app.current_user_id": (
        "fixed",
        "each state binds its own actor; that actor's rows are the varied inputs",
    ),
    **{
        f"column:load_current_user:identity_user.{name}": ("fixed", _USER_ROW)
        for name in (
            "date_joined",
            "email",
            "first_name",
            "last_login",
            "last_name",
            "password",
            "username",
        )
    },
    "column:has_permission:identity_clinic.id": ("fixed", _CLINIC),
    "column:has_permission:identity_clinic.organization_id": ("fixed", _CLINIC),
    "column:has_permission:identity_clinic.crm_uf": ("fixed", _CLINIC),
    **{
        f"column:has_permission:intake_patientclinicenrollment.{name}": (
            "fixed",
            _ENROLLMENT,
        )
        for name in ("id", "clinic_id", "organization_id", "patient_id")
    },
    "column:has_permission:identity_userclinicrole.id": ("fixed", _KEY),
    "column:has_permission:identity_userclinicrole.user_id": ("fixed", _KEY),
    "column:has_permission:identity_userclinicrole.role": (
        "varied",
        frozenset(
            role_label(roles)
            for size in range(len(ROLES) + 1)
            for roles in combinations(ROLES, size)
        ),
    ),
    "column:has_permission:identity_userclinicrole.clinic_id": ("varied", _PLACE),
    "column:has_permission:identity_userclinicrole.organization_id": (
        "varied",
        frozenset({"this_organization", "other_organization"}),
    ),
    "column:has_permission:identity_rolegrant.id": ("fixed", _KEY),
    "column:has_permission:identity_rolegrant.organization_id": (
        "fixed",
        "a grant belongs to its clinic's organization; clinic_id is varied",
    ),
    "column:has_permission:identity_rolegrant.clinic_id": ("varied", _PLACE),
    "column:has_permission:identity_rolegrant.role": ("varied", frozenset(ROLES)),
    "column:has_permission:identity_rolegrant.permission": (
        "varied",
        frozenset(f"{r}:{p}" for r in ROLES for p in BUNDLES_V1[r]),
    ),
    "column:has_permission:identity_rolegrant.valid_from": ("varied", _WINDOW),
    "column:has_permission:identity_rolegrant.valid_to": ("varied", _WINDOW),
    "column:has_permission:identity_rolegrant.effect": (
        "fixed",
        "check constraint identity_rolegrant_remove_only admits only 'remove'",
    ),
    "column:has_permission:identity_rolegrant.bundle_version": (
        "fixed",
        "check constraint identity_rolegrant_remove_only admits only 1",
    ),
    "column:has_permission:identity_professionalregistration.id": ("fixed", _KEY),
    "column:has_permission:identity_professionalregistration.organization_id": (
        "fixed",
        _KEY,
    ),
    "column:has_permission:identity_professionalregistration.user_id": ("fixed", _KEY),
    "column:has_permission:identity_professionalregistration.clinic_id": (
        "varied",
        _PLACE,
    ),
    "column:has_permission:identity_professionalregistration.role": (
        "varied",
        frozenset({"held", "not_held"}),
    ),
    "column:has_permission:identity_professionalregistration.jurisdiction": (
        "varied",
        frozenset({"clinic_uf", "other_uf"}),
    ),
    "column:has_permission:identity_professionalregistration.status": (
        "varied",
        frozenset(STATUSES),
    ),
    "column:has_permission:identity_professionalregistration.synthetic": (
        "fixed",
        "check constraint identity_registration_synthetic requires true",
    ),
    "column:has_permission:identity_professionalregistration.revoked_at": (
        "varied",
        frozenset({"null", "set"}),
    ),
    "column:has_permission:identity_professionalregistration.valid_from": (
        "varied",
        _BOUNDED,
    ),
    "column:has_permission:identity_professionalregistration.valid_to": (
        "varied",
        _BOUNDED,
    ),
    "column:has_permission:identity_professionalregistration.physician_profile_id": (
        "varied",
        frozenset({"null", "linked"}),
    ),
    "column:has_permission:identity_physicianprofile.id": ("fixed", _KEY),
    "column:has_permission:identity_physicianprofile.organization_id": ("fixed", _KEY),
    "column:has_permission:identity_physicianprofile.user_id": (
        "varied",
        frozenset({"actor", "other_user"}),
    ),
    "column:has_permission:identity_physicianprofile.jurisdiction": (
        "varied",
        frozenset({"clinic_uf", "other_uf"}),
    ),
    "column:has_permission:identity_physicianprofile.synthetic": (
        "varied",
        frozenset({"true", "false"}),
    ),
    "column:has_permission:identity_physicianprofile.status": (
        "varied",
        frozenset(STATUSES),
    ),
    "column:has_permission:identity_physicianprofile.last_checked_at": (
        "varied",
        _MOMENT,
    ),
    "column:has_permission:identity_physicianprofile.recheck_at": ("varied", _MOMENT),
    "column:has_permission:identity_physicianprofile.expires_at": ("varied", _MOMENT),
    "column:has_permission:identity_careteammembership.id": ("fixed", _KEY),
    "column:has_permission:identity_careteammembership.organization_id": (
        "fixed",
        _KEY,
    ),
    "column:has_permission:identity_careteammembership.user_id": ("fixed", _KEY),
    "column:has_permission:identity_careteammembership.clinic_id": ("varied", _PLACE),
    "column:has_permission:identity_careteammembership.patient_enrollment_id": (
        "varied",
        frozenset({"probed", "other"}),
    ),
    "column:has_permission:identity_careteammembership.role": (
        "varied",
        frozenset({"held", "not_held"}),
    ),
    "column:has_permission:identity_careteammembership.revoked_at": (
        "varied",
        frozenset({"null", "set"}),
    ),
    "column:has_permission:identity_careteammembership.valid_from": ("varied", _WINDOW),
    "column:has_permission:identity_careteammembership.valid_to": ("varied", _WINDOW),
    "column:has_permission:ehr_encounter.id": ("fixed", _KEY),
    "column:has_permission:ehr_encounter.organization_id": ("fixed", _KEY),
    "column:has_permission:ehr_encounter.clinic_id": ("fixed", _KEY),
    "column:has_permission:ehr_encounter.patient_id": ("fixed", _KEY),
    "column:has_permission:ehr_encounter.physician_id": (
        "varied",
        frozenset({"actor", "other"}),
    ),
    "column:has_permission:ehr_encounter.state": (
        "varied",
        frozenset({"open", "closed"}),
    ),
}


def _window_label(
    valid_from: datetime, valid_to: datetime | None, now: datetime
) -> str:
    if valid_from > now:
        return "future"
    if valid_to is not None and valid_to <= now:
        return "expired"
    return "open" if valid_to is None else "current"


def realized(matrix: Matrix) -> dict[str, set[str]]:
    """Read back, from the written rows, the values each varied input took."""
    graph = matrix.places.graph
    actors = {state.actor for state in matrix.states}
    seen: dict[str, set[str]] = {
        key: set() for key, (kind, _) in DIMENSIONS.items() if kind == "varied"
    }

    def add(column: str, value: str) -> None:
        keys = [key for key in seen if key.endswith(f":{column}")]
        assert keys, column
        for key in keys:
            seen[key].add(value)

    for row in User.objects.filter(pk__in=actors).values("is_active", *USER_FLAGS):
        for column, value in row.items():
            add(f"identity_user.{column}", "true" if value else "false")
    held: dict[UUID, set[str]] = {actor: set() for actor in actors}
    for organization in (graph.organization_a, graph.organization_b):
        with owner_context(organization):
            for user, clinic, role, org in UserClinicRole.objects.filter(
                user_id__in=actors
            ).values_list("user_id", "clinic_id", "role", "organization_id"):
                if clinic == graph.clinic_a:
                    held[user].add(role)
                add(
                    "identity_userclinicrole.clinic_id",
                    "this_clinic" if clinic == graph.clinic_a else "other_clinic",
                )
                add(
                    "identity_userclinicrole.organization_id",
                    "this_organization"
                    if org == graph.organization_a
                    else "other_organization",
                )
    for roles in held.values():
        add("identity_userclinicrole.role", role_label(roles))
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.statement_timestamp()")
        row = cursor.fetchone()
        assert row is not None
        now: datetime = row[0]
        _realized_grants(graph, now, add)
        _realized_credentials(matrix, held, now, add)
        cursor.execute(
            "SELECT physician_id FROM clinic_app.ehr_encounter "
            "WHERE clinic_id = %s AND state = 'open'",
            [graph.clinic_a],
        )
        physicians = {physician for (physician,) in cursor.fetchall()}
        _realized_encounters(cursor, graph, actors, add)
    for actor in actors:
        add("ehr_encounter.physician_id", "actor" if actor in physicians else "other")
    return seen


def _realized_encounters(
    cursor: CursorWrapper,
    graph: RbacGraph,
    actors: set[UUID],
    add: Callable[[str, str], None],
) -> None:
    cursor.execute(
        "SELECT state FROM clinic_app.ehr_encounter "
        "WHERE clinic_id = %s AND physician_id = ANY(%s)",
        [graph.clinic_a, list(actors)],
    )
    for (state,) in cursor.fetchall():
        add("ehr_encounter.state", str(state))


def _realized_grants(
    graph: RbacGraph, now: datetime, add: Callable[[str, str], None]
) -> None:
    for clinic, role, permission, valid_from, valid_to in RoleGrant.objects.values_list(
        "clinic_id", "role", "permission", "valid_from", "valid_to"
    ):
        add(
            "identity_rolegrant.clinic_id",
            "this_clinic" if clinic == graph.clinic_a else "other_clinic",
        )
        add("identity_rolegrant.role", role)
        add("identity_rolegrant.permission", f"{role}:{permission}")
        window = _window_label(valid_from, valid_to, now)
        add("identity_rolegrant.valid_from", window)
        add("identity_rolegrant.valid_to", window)


def _realized_credentials(
    matrix: Matrix,
    held: Mapping[UUID, set[str]],
    now: datetime,
    add: Callable[[str, str], None],
) -> None:
    graph = matrix.places.graph
    actors = set(held)
    registrations = ProfessionalRegistration.objects.filter(user_id__in=actors).defer(
        "number", "specialty"
    )
    for row in registrations:
        prefix = "identity_professionalregistration"
        add(
            f"{prefix}.clinic_id",
            "this_clinic" if row.clinic_id == graph.clinic_a else "other_clinic",
        )
        add(f"{prefix}.role", "held" if row.role in held[row.user_id] else "not_held")
        add(
            f"{prefix}.jurisdiction",
            "clinic_uf" if row.jurisdiction == "SP" else "other_uf",
        )
        add(f"{prefix}.status", row.status)
        add(f"{prefix}.revoked_at", "null" if row.revoked_at is None else "set")
        window = _window_label(row.valid_from, row.valid_to, now)
        add(f"{prefix}.valid_from", window)
        add(f"{prefix}.valid_to", window)
        add(
            f"{prefix}.physician_profile_id",
            "null" if row.physician_profile_id is None else "linked",
        )
        if row.physician_profile_id is None:
            continue
        profile = PhysicianProfile.objects.get(pk=row.physician_profile_id)
        prefix = "identity_physicianprofile"
        add(
            f"{prefix}.user_id",
            "actor" if profile.user_id == row.user_id else "other_user",
        )
        add(
            f"{prefix}.jurisdiction",
            "clinic_uf" if profile.jurisdiction == row.jurisdiction else "other_uf",
        )
        add(f"{prefix}.synthetic", "true" if profile.synthetic else "false")
        add(f"{prefix}.status", profile.status)
        for column in ("last_checked_at", "recheck_at", "expires_at"):
            moment = getattr(profile, column)
            add(
                f"{prefix}.{column}",
                "null" if moment is None else "past" if moment <= now else "future",
            )
    probed = set(matrix.places.enrollments)
    for scope in CareTeamMembership.objects.filter(user_id__in=actors):
        prefix = "identity_careteammembership"
        add(
            f"{prefix}.clinic_id",
            "this_clinic" if scope.clinic_id == graph.clinic_a else "other_clinic",
        )
        add(
            f"{prefix}.patient_enrollment_id",
            "probed" if scope.patient_enrollment_id in probed else "other",
        )
        add(
            f"{prefix}.role",
            "held" if scope.role in held[scope.user_id] else "not_held",
        )
        add(f"{prefix}.revoked_at", "null" if scope.revoked_at is None else "set")
        window = _window_label(scope.valid_from, scope.valid_to, now)
        add(f"{prefix}.valid_from", window)
        add(f"{prefix}.valid_to", window)
