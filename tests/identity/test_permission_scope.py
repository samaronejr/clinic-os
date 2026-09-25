"""Database-enforced subtraction, professional scope and revocation races."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from apps.identity.permissions import BUNDLES_V1, PERMISSIONS
from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone
from psycopg.errors import CheckViolation

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from tenant_probe_support import assert_no_cross_tenant_rows

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TABLES = {
    "identity_rolegrant",
    "identity_careteammembership",
    "identity_professionalregistration",
}


def remove_permission(graph: RbacGraph, role: str, permission: str) -> RoleGrant:
    return RoleGrant.objects.create(
        organization_id=graph.organization_a,
        clinic_id=graph.clinic_a,
        role=role,
        permission=permission,
        valid_from=timezone.now() - timedelta(days=1),
    )


@pytest.mark.parametrize(
    "role", ["allied_professional", "scheduler", "owner", "clinic_admin"]
)
def test_alias_roles_are_checked_for_every_permission(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor, enrollment = permission_actor(rbac_graph, role)
    with permission_context(rbac_graph, actor):
        for permission in sorted(PERMISSIONS):
            if permission in BUNDLES_V1[role]:
                assert (
                    require_permission(
                        permission,
                        clinic_id=rbac_graph.clinic_a,
                        patient_enrollment_id=enrollment,
                    )
                    == actor
                )
            else:
                with pytest.raises(CurrentActorError):
                    require_permission(
                        permission,
                        clinic_id=rbac_graph.clinic_a,
                        patient_enrollment_id=enrollment,
                    )


@pytest.mark.parametrize("effect", ["add", "allow", "", "REMOVE"])
def test_remove_only_constraint_rejects_additions(
    rbac_graph: RbacGraph, effect: str
) -> None:
    with owner_context(rbac_graph.organization_a):
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            RoleGrant.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                role="finance",
                permission="charge.read",
                effect=effect,
                valid_from=timezone.now(),
            )
        cause = error.value.__cause__
        assert isinstance(cause, CheckViolation)
        assert cause.diag.constraint_name == "identity_rolegrant_remove_only"


def test_override_cannot_name_a_permission_outside_the_bundle(
    rbac_graph: RbacGraph,
) -> None:
    with owner_context(rbac_graph.organization_a):
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            remove_permission(rbac_graph, "finance", "clinical.read")
        cause = error.value.__cause__
        assert isinstance(cause, CheckViolation)
        assert cause.diag.constraint_name == "identity_rolegrant_bundle"


@pytest.mark.parametrize(
    "change", ["role", "care", "professional", "override", "inactive"]
)
def test_committed_revocation_is_seen_mid_request(
    rbac_graph: RbacGraph, change: str
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "physician")
    ready = Barrier(2, timeout=15)
    committed = Barrier(2, timeout=15)

    def revoke() -> None:
        try:
            ready.wait()
            with owner_context(rbac_graph.organization_a):
                if change == "role":
                    UserClinicRole.objects.filter(user_id=actor).delete()
                elif change == "care":
                    CareTeamMembership.objects.filter(user_id=actor).update(
                        revoked_at=timezone.now()
                    )
                elif change == "professional":
                    ProfessionalRegistration.objects.filter(user_id=actor).update(
                        revoked_at=timezone.now()
                    )
                elif change == "inactive":
                    User.objects.filter(pk=actor).update(is_active=False)
                else:
                    remove_permission(rbac_graph, "physician", "clinical.read")
            committed.wait()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(revoke)
        with permission_context(rbac_graph, actor):
            assert (
                require_permission(
                    "clinical.read",
                    clinic_id=rbac_graph.clinic_a,
                    patient_enrollment_id=enrollment,
                )
                == actor
            )
            ready.wait()
            committed.wait()
            with pytest.raises(CurrentActorError):
                require_permission(
                    "clinical.read",
                    clinic_id=rbac_graph.clinic_a,
                    patient_enrollment_id=enrollment,
                )
        worker.result(timeout=15)


@pytest.mark.parametrize(
    "bad", ["", "CLINICAL.READ", "clinical.read' OR true --", "*", "a" * 1000]
)
def test_unknown_and_malformed_permissions_deny_without_reflecting_input(
    rbac_graph: RbacGraph, bad: str
) -> None:
    with permission_context(rbac_graph, rbac_graph.physician):
        with pytest.raises(CurrentActorError) as error:
            require_permission(bad, clinic_id=rbac_graph.clinic_a)
        assert error.value.args == ("current actor unauthorized",)


def test_malformed_selectors_fail_before_database_cast(rbac_graph: RbacGraph) -> None:
    with permission_context(rbac_graph, rbac_graph.physician):
        for clinic, enrollment in (
            ("bad", None),
            (None, None),
            (rbac_graph.clinic_a, "bad"),
        ):
            with pytest.raises(CurrentActorError):
                require_permission(
                    "clinical.read",
                    clinic_id=cast("UUID", clinic),
                    patient_enrollment_id=cast("UUID | None", enrollment),
                )
        with pytest.raises(CurrentActorError):
            require_permission(
                cast("str", cast("object", [])), clinic_id=rbac_graph.clinic_a
            )


def test_missing_care_or_wrong_clinic_is_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, actor):
        for clinic, subject in (
            (rbac_graph.clinic_a, None),
            (rbac_graph.clinic_a, uuid4()),
            (rbac_graph.clinic_b, enrollment),
            (rbac_graph.clinic_c, enrollment),
        ):
            with pytest.raises(CurrentActorError) as error:
                require_permission(
                    "clinical.read", clinic_id=clinic, patient_enrollment_id=subject
                )
            assert error.value.args == ("current actor unauthorized",)


def test_narrowing_is_clinic_local_and_not_an_alternative_membership(
    rbac_graph: RbacGraph,
) -> None:
    actor, _ = permission_actor(rbac_graph, "finance")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=actor,
            role="finance",
        )
        remove_permission(rbac_graph, "finance", "charge.read")
    with permission_context(rbac_graph, actor):
        with pytest.raises(CurrentActorError):
            require_permission("charge.read", clinic_id=rbac_graph.clinic_a)
        assert require_permission("charge.read", clinic_id=rbac_graph.clinic_b) == actor
        with pytest.raises(CurrentActorError):
            require_permission("charge.read", clinic_id=rbac_graph.clinic_c)


def test_scope_history_and_revocation_are_immutable(rbac_graph: RbacGraph) -> None:
    actor, _ = permission_actor(rbac_graph, "nurse")
    with owner_context(rbac_graph.organization_a):
        grant = remove_permission(rbac_graph, "nurse", "appointment.read")
        for model, pk in (
            (RoleGrant, grant.pk),
            (CareTeamMembership, CareTeamMembership.objects.get(user_id=actor).pk),
            (
                ProfessionalRegistration,
                ProfessionalRegistration.objects.get(user_id=actor).pk,
            ),
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=pk).delete()
            with pytest.raises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=pk).update(role="physician")
        CareTeamMembership.objects.filter(user_id=actor).update(
            revoked_at=timezone.now()
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            CareTeamMembership.objects.filter(user_id=actor).update(revoked_at=None)
        assert User.objects.filter(pk=actor).exists()


def test_professional_evidence_is_encrypted_synthetic_and_council_scoped(
    rbac_graph: RbacGraph,
) -> None:
    actor, _ = permission_actor(rbac_graph, "nurse")
    with owner_context(rbac_graph.organization_a):
        registration = ProfessionalRegistration.objects.get(user_id=actor)
        assert registration.number == "SINTETICO-001"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT number, specialty FROM "
                "clinic_app.identity_professionalregistration WHERE id=%s",
                [registration.pk],
            )
            row = cursor.fetchone()
        assert b"SINTETICO" not in bytes(row[0])
        assert b"Sintetico" not in bytes(row[1])
        registration.pk = uuid4()
        registration.synthetic = False
        with pytest.raises(IntegrityError), transaction.atomic():
            registration.save(force_insert=True)
        registration.synthetic = True
        registration.council = "CRM"
        with pytest.raises(IntegrityError), transaction.atomic():
            registration.save(force_insert=True)


def test_exact_policy_and_runtime_acl(rbac_graph: RbacGraph) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename, policyname, cmd, roles FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [list(TABLES)],
        )
        assert {(t, p, c, tuple(r)) for t, p, c, r in cursor.fetchall()} == {
            (table, "scope_provision", "ALL", ("clinic_owner",)) for table in TABLES
        } | {(table, "scope_read", "SELECT", ("clinic_app",)) for table in TABLES}
        cursor.execute(
            "SELECT table_name, privilege_type FROM "
            "information_schema.role_table_grants WHERE grantee='clinic_app' "
            "AND table_schema='clinic_app' AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {(table, "SELECT") for table in TABLES}


def test_new_tables_have_nonvacuous_cross_tenant_probes(
    tenant_probe_pair: RbacGraph,
) -> None:
    graph = tenant_probe_pair
    other = replace(graph, organization_a=graph.organization_b, clinic_a=graph.clinic_c)
    for scope in (graph, other):
        permission_actor(scope, "nurse")
        with owner_context(scope.organization_a):
            UserClinicRole.objects.create(
                organization_id=scope.organization_a,
                clinic_id=scope.clinic_a,
                user_id=graph.shared_user,
                role="clinic_manager",
            )
            remove_permission(scope, "finance", "charge.read")
    for model in (RoleGrant, CareTeamMembership, ProfessionalRegistration):
        assert_no_cross_tenant_rows(graph, model)


@pytest.mark.parametrize("kind", ["care", "professional"])
@pytest.mark.parametrize("window", ["future", "expired"])
def test_scope_windows_fail_closed(
    rbac_graph: RbacGraph,
    kind: str,
    window: str,
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "nurse")
    model = CareTeamMembership if kind == "care" else ProfessionalRegistration
    with owner_context(rbac_graph.organization_a):
        record = model.objects.get(user_id=actor)
        model.objects.filter(pk=record.pk).update(revoked_at=timezone.now())
        record.pk = uuid4()
        shift = timedelta(days=2 if window == "future" else -2)
        record.valid_from = timezone.now() + shift
        record.valid_to = record.valid_from + timedelta(days=1)
        record.save(force_insert=True)
    with permission_context(rbac_graph, actor), pytest.raises(CurrentActorError):
        require_permission(
            "observation.write",
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment_id=enrollment,
        )


@pytest.mark.parametrize("window", ["future", "expired"])
def test_inactive_removal_windows_do_not_remove_permissions(
    rbac_graph: RbacGraph,
    window: str,
) -> None:
    actor, _ = permission_actor(rbac_graph, "finance")
    shift = timedelta(days=2 if window == "future" else -2)
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="finance",
            permission="charge.read",
            valid_from=timezone.now() + shift,
            valid_to=timezone.now() + shift + timedelta(days=1),
        )
    with permission_context(rbac_graph, actor):
        assert require_permission("charge.read", clinic_id=rbac_graph.clinic_a) == actor


@pytest.mark.parametrize("guc", ["app.current_user_id", "app.current_tenant"])
@pytest.mark.parametrize("value", ["", "malformed", str(UUID(int=0))])
def test_sql_helper_fails_closed_for_missing_or_hostile_authority(
    rbac_graph: RbacGraph,
    guc: str,
    value: str,
) -> None:
    with (
        permission_context(rbac_graph, rbac_graph.shared_user),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT set_config(%s,%s,true)", [guc, value])
        cursor.execute(
            "SELECT clinic_app.has_permission(%s,%s,NULL)",
            ["appointment.book", rbac_graph.clinic_a],
        )
        assert cursor.fetchone() == (False,)


def test_care_scope_cannot_bind_another_clinics_enrollment(
    rbac_graph: RbacGraph,
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "nurse")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            user_id=actor,
            clinic_id=rbac_graph.clinic_b,
            organization_id=rbac_graph.organization_a,
            role="nurse",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            CareTeamMembership.objects.create(
                user_id=actor,
                clinic_id=rbac_graph.clinic_b,
                organization_id=rbac_graph.organization_a,
                role="nurse",
                patient_enrollment_id=enrollment,
                valid_from=timezone.now(),
            )


def test_concurrent_removals_cannot_overwrite_each_other(rbac_graph: RbacGraph) -> None:
    actor, _ = permission_actor(rbac_graph, "finance")
    barrier = Barrier(2, timeout=15)
    permissions = ("charge.read", "refund.approve")

    def narrow(permission: str) -> UUID:
        try:
            with owner_context(rbac_graph.organization_a):
                barrier.wait()
                grant = remove_permission(rbac_graph, "finance", permission)
            return grant.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(narrow, permissions))) == 2
    with permission_context(rbac_graph, actor):
        for permission in permissions:
            with pytest.raises(CurrentActorError):
                require_permission(permission, clinic_id=rbac_graph.clinic_a)
