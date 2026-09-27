"""Small adversarial backstop, not the nonstaff certificate.

The certificate is authority-channel observation. These named cases retain role
unions, scope and lifecycle regression examples without claiming exhaustive
coverage. Canonical constraints, RLS and immutable-history triggers stay live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid5

from apps.identity.models import (
    CareTeamMembership,
    PhysicianProfile,
    ProfessionalRegistration,
    RoleGrant,
    UserClinicRole,
)
from django.db import connection
from django.utils import timezone

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.identity.models import User

ROLES = tuple(UserClinicRole.Role.values)
ROLE_CASES = (
    (),
    *((role,) for role in ROLES),
    ("owner", "physician"),
    ("owner", "physician", "clinic_admin"),
    ("nurse", "scheduler", "finance"),
    ("receptionist", "org_admin"),
)
CLINIC_LAYOUTS = ("target", "other", "both")
CARE_LIFECYCLES = ("active", "revoked", "expired", "future")
CARE_PATIENT_SCOPES = ("target", "other")
CLINICAL_ROLES = ("physician", "nurse", "allied_professional")


@dataclass(frozen=True)
class CareScope:
    enrollment: UUID
    other_enrollment: UUID


@dataclass(frozen=True)
class ReplayScope:
    clinic: UUID
    organization: UUID
    other_clinic: UUID | None = None
    care: CareScope | None = None


@dataclass(frozen=True)
class StaffState:
    roles: tuple[str, ...]
    layout: str = "target"
    care: str = "absent"
    care_patient: str = "target"
    authority: str = "baseline"

    def key(self) -> str:
        return (
            f"{self.layout}/{self.care}/{self.care_patient}/{self.authority}/"
            + ",".join(self.roles)
        )


def backstop_states(scope: ReplayScope) -> Iterator[StaffState]:
    yield from (StaffState(roles) for roles in ROLE_CASES)
    if scope.other_clinic is not None:
        yield StaffState(("owner",), layout="other")
        yield StaffState(("owner", "physician"), layout="both")
    if scope.care is not None:
        for life in CARE_LIFECYCLES:
            yield StaffState(CLINICAL_ROLES, care=life)
        yield StaffState(CLINICAL_ROLES, care="active", care_patient="other")
        for life in ("revoked", "expired"):
            yield StaffState(("receptionist", "org_admin"), care=life)
        yield StaffState(("physician",), care="active", authority="professional")
    yield StaffState(("owner",), authority="rolegrant")
    yield StaffState(("physician",), authority="inactive")


def set_memberships(actor: User, scope: ReplayScope, state: StaffState) -> None:
    """One committed fixture transaction per subset, shared by all scenarios."""
    clinics = [scope.clinic]
    if state.layout != "target":
        assert scope.other_clinic is not None
        clinics = (
            [scope.other_clinic]
            if state.layout == "other"
            else [scope.clinic, scope.other_clinic]
        )
    assignments = [(c, role) for c in clinics for role in state.roles]
    with owner_context(scope.organization):
        UserClinicRole.objects.filter(user=actor).delete()
        UserClinicRole.objects.bulk_create(
            [
                UserClinicRole(
                    id=uuid5(actor.pk, f"{c}/{role}"),
                    user=actor,
                    organization_id=scope.organization,
                    clinic_id=c,
                    role=role,
                )
                for c, role in assignments
            ]
        )
        actual = set(
            UserClinicRole.objects.filter(user=actor).values_list("clinic_id", "role")
        )
        assert actual == set(assignments)


def _reset_care(actor: User, organization: UUID) -> None:
    with owner_context(organization), connection.cursor() as cursor:
        # Inspect every tenant before fixture cleanup. The resolver already has
        # SELECT/BYPASSRLS; this neither grants authority nor bypasses a guard.
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT DISTINCT user_id FROM clinic_app.identity_careteammembership"
        )
        assert {row[0] for row in cursor.fetchall()} <= {actor.pk}
        cursor.execute("SET LOCAL ROLE clinic_owner")
        cursor.execute("TRUNCATE clinic_app.identity_careteammembership")


def set_care_profile(actor: User, scope: ReplayScope, life: str, patient: str) -> None:
    assert scope.care is not None
    _reset_care(actor, scope.organization)
    if life == "absent":
        return
    # Every care row must originate while its canonical clinical role exists.
    # Subsequent canonical revocation may legally leave historical care rows.
    set_memberships(actor, scope, StaffState(CLINICAL_ROLES))
    now = timezone.now()
    start, end = now - timedelta(days=7), now + timedelta(days=7)
    if life == "expired":
        start, end = now - timedelta(days=14), now - timedelta(days=7)
    elif life == "future":
        start, end = now + timedelta(days=7), now + timedelta(days=14)
    enrollment = (
        scope.care.enrollment if patient == "target" else scope.care.other_enrollment
    )
    with owner_context(scope.organization):
        CareTeamMembership.objects.bulk_create(
            [
                CareTeamMembership(
                    id=uuid5(actor.pk, f"care/{role}"),
                    organization_id=scope.organization,
                    clinic_id=scope.clinic,
                    patient_enrollment_id=enrollment,
                    user=actor,
                    role=role,
                    valid_from=start,
                    valid_to=end,
                )
                for role in CLINICAL_ROLES
            ]
        )
        if life == "revoked":
            # The real immutable-history trigger permits exactly this change.
            CareTeamMembership.objects.filter(user=actor).update(revoked_at=now)
        rows = list(CareTeamMembership.objects.filter(user=actor))
        assert {row.role for row in rows} == set(CLINICAL_ROLES)
        assert all((row.revoked_at is not None) == (life == "revoked") for row in rows)
        assert all(row.patient_enrollment_id == enrollment for row in rows)


def add_authority_backstop(actor: User, scope: ReplayScope, state: StaffState) -> None:
    now = timezone.now()
    with owner_context(scope.organization):
        if state.authority == "rolegrant":
            RoleGrant.objects.create(
                organization_id=scope.organization,
                clinic_id=scope.clinic,
                role="owner",
                permission="appointment.read",
                valid_from=now - timedelta(days=1),
            )
        elif state.authority == "professional":
            profile = PhysicianProfile.objects.create(
                organization_id=scope.organization,
                user=actor,
                jurisdiction="SP",
                registration_number="SINTETICO-" + actor.pk.hex,
                signing_subject="Sintetico",
                status="regular",
                synthetic=True,
                last_checked_at=now - timedelta(days=1),
                recheck_at=now + timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
            ProfessionalRegistration.objects.create(
                organization_id=scope.organization,
                clinic_id=scope.clinic,
                user=actor,
                physician_profile=profile,
                role="physician",
                council="CRM",
                number="SINTETICO-OBSERVER",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )


def assert_membership_schema() -> None:
    # Fixture hygiene only; live channel derivation is the authority-input gate.
    assert {f.name for f in UserClinicRole._meta.fields} == {
        "id",
        "user",
        "organization",
        "clinic",
        "role",
    }
    assert not UserClinicRole._meta.get_field("clinic").null
    assert (
        tuple(
            value
            for value, _label in UserClinicRole._meta.get_field("role").choices or ()
        )
        == ROLES
    )
