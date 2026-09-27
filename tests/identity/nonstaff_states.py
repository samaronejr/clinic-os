"""Finite, model-backed staff-state domain for differential execution.

Canonical role revocation deletes a UserClinicRole row. There is no per-row
active, revoked or organization-wide assignment flag. Account authentication
(User.is_active) is held fixed, not confused with membership activity.

Care history is immutable. Between synthetic care profiles only, reset the
otherwise empty owned fixture table, as Django's transaction-test flush does.
Never disable a trigger or change an authorization function/grant for setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from typing import TYPE_CHECKING
from uuid import uuid5

from apps.identity.models import CareTeamMembership, UserClinicRole
from django.db import connection
from django.utils import timezone

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.identity.models import User

ROLES = tuple(UserClinicRole.Role.values)
ROLE_SUBSETS = tuple(
    subset for size in range(len(ROLES) + 1) for subset in combinations(ROLES, size)
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

    def key(self) -> str:
        return f"{self.layout}/{self.care}/{self.care_patient}/" + ",".join(self.roles)


def profiles(
    other_clinic: UUID | None, care_scope: CareScope | None
) -> Iterator[tuple[str, str, str]]:
    care_profiles = [("absent", "target")]
    if care_scope is not None:
        care_profiles.extend(
            (life, patient)
            for life in CARE_LIFECYCLES
            for patient in CARE_PATIENT_SCOPES
        )
    for care, patient in care_profiles:
        for layout in CLINIC_LAYOUTS if other_clinic is not None else ("target",):
            yield layout, care, patient


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


def assert_membership_schema() -> None:
    # A future lifecycle/scope field must expand the generator, not silently
    # inherit today's documented structural equivalences.
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
