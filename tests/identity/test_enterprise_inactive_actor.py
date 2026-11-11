"""Raw enterprise SQL keeps inactive staff and mixed directory actors closed."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.enterprise import approve_group_mapping
from apps.identity.enterprise_access import (
    EmergencyScope,
    grant_break_glass,
    review_break_glass,
    revoke_break_glass,
)
from apps.identity.enterprise_support_access import approve_support_access
from apps.identity.models import User
from apps.identity.scim import create_resource, scim_context
from apps.identity.session_inventory import session_hash
from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone
from psycopg.errors import InsufficientPrivilege

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_sso_account_sql_refuses_its_inactive_directory_user(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-inactive", "externalId": "inactive-sub"},
        )
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={
                "displayName": "Synthetic reception",
                "members": [{"value": str(user.pk)}],
            },
        )
    assert user.user_id is not None
    assert user.external_hash is not None
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        approve_group_mapping(
            clinic_id=rbac_graph.clinic_a,
            group_id=group.pk,
            unit_id=rbac_graph.clinic_a,
            bundle="receptionist",
            session_key=setup.session_key,
        )
    for active in (True, False):
        if not active:
            with owner_context(rbac_graph.organization_a):
                User.objects.filter(pk=user.user_id).update(is_active=False)
        with (
            permission_context(rbac_graph, user.user_id),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT set_config('app.current_sso_connection',%s,true)",
                [str(setup.connection.pk)],
            )
            cursor.execute(
                "SELECT user_id FROM clinic_app.enterprise_sso_account(%s,%s)",
                [setup.connection.pk, bytes(user.external_hash)],
            )
            assert bool(cursor.fetchall()) is active


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_scope_and_raw_guard_refuse_an_inactive_named_actor(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.clinic_admin,
            purpose="Synthetic inactive-actor probe",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=setup.session_key,
        )
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT purpose FROM clinic_app.identity_supportaccessgrant WHERE id=%s",
            [grant.pk],
        )
        row = cursor.fetchone()
        assert row is not None
        (purpose,) = row
    for active in (True, False):
        if not active:
            with owner_context(rbac_graph.organization_a):
                User.objects.filter(pk=rbac_graph.clinic_admin).update(is_active=False)
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.enterprise_support_scope(%s,%s,%s)",
                [grant.pk, rbac_graph.clinic_a, session_hash(setup.session_key)],
            )
            assert bool(cursor.fetchall()) is active
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.enterprise_factor',%s,true)",
                [session_hash(setup.session_key).hex()],
            )
        with (
            pytest.raises(DatabaseError) as refused,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO clinic_app.identity_supportaccessgrant "
                "(id,organization_id,clinic_id,support_user_id,approved_by_id,"
                "purpose,expires_at,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    uuid4(),
                    rbac_graph.organization_a,
                    rbac_graph.clinic_a,
                    rbac_graph.clinic_admin,
                    rbac_graph.clinic_admin,
                    purpose,
                    timezone.now() + timedelta(hours=1),
                    timezone.now(),
                ],
            )
        assert isinstance(refused.value.__cause__, InsufficientPrivilege)
        context = refused.value.__cause__.diag.context
        assert context is not None
        assert "enterprise_governance_guard" in context


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_emergency_and_task_sql_refuse_inactive_clinician_and_manager(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    with permission_context(rbac_graph, clinician):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="Synthetic inactive-actor probe",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(client),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.enterprise_break_glass_usable(%s,%s)",
                [grant.pk, rbac_graph.clinic_a],
            )
            assert cursor.fetchone() == (True,)
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=clinician).update(is_active=False)
    with permission_context(rbac_graph, clinician), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.enterprise_break_glass_usable(%s,%s)",
            [grant.pk, rbac_graph.clinic_a],
        )
        assert cursor.fetchone() == (False,)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        revoke_break_glass(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=setup.session_key,
        )
        with transaction.atomic():
            review_break_glass(
                clinic_id=rbac_graph.clinic_a,
                grant_id=grant.pk,
                session_key=setup.session_key,
            )
            transaction.set_rollback(True)
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=rbac_graph.clinic_admin).update(is_active=False)
    with (
        permission_context(rbac_graph, rbac_graph.clinic_admin),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT set_config('app.enterprise_factor',%s,true)",
            [session_hash(setup.session_key).hex()],
        )
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            cursor.execute("SELECT clinic_app.enterprise_finish_review(%s)", [grant.pk])
        assert isinstance(refused.value.__cause__, InsufficientPrivilege)
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            cursor.execute(
                "SELECT clinic_app.enterprise_security_task(NULL::jsonb,'review')"
            )
        assert isinstance(refused.value.__cause__, InsufficientPrivilege)
        with transaction.atomic():
            cursor.execute(
                "UPDATE clinic_app.workflows_task "
                "SET owner_user_id=%s,state='assigned',revision=revision+1,"
                "last_command_key=%s,last_command_digest=%s,"
                "updated_at=statement_timestamp() WHERE id=%s",
                [
                    rbac_graph.clinic_admin,
                    uuid4(),
                    "a" * 64,
                    grant.review_task_id,
                ],
            )
            assert cursor.rowcount == 0


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_security_task_sql_rechecks_its_actual_manager(rbac_graph: RbacGraph) -> None:
    setup = enterprise_setup(rbac_graph)
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    with permission_context(rbac_graph, clinician):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="Synthetic private review-helper probe",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(client),
        )
    for active in (True, False):
        if not active:
            with owner_context(rbac_graph.organization_a):
                User.objects.filter(pk=rbac_graph.clinic_admin).update(is_active=False)
        with (
            owner_context(rbac_graph.organization_a),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true),"
                "set_config('app.enterprise_factor',%s,true)",
                [
                    str(rbac_graph.clinic_admin),
                    session_hash(setup.session_key).hex(),
                ],
            )
            # This private helper runs as the real resolver, never as clinic_app.
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                "SELECT clinic_app.enterprise_security_task("
                "to_jsonb(candidate),'review') "
                "FROM clinic_app.workflows_task candidate WHERE id=%s",
                [grant.review_task_id],
            )
            assert cursor.fetchone() == (active,)
            cursor.execute("RESET ROLE")


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_scim_audit_scope_requires_absent_even_inactive_staff_actor(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=rbac_graph.physician).update(is_active=False)
    with (
        runtime_role(),
        scim_context(bearer=setup.token) as scope,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT principal_id FROM clinic_app.enterprise_scim_audit_scope(%s)",
            [scope.clinic_id],
        )
        assert len(cursor.fetchall()) == 1
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true)",
            [str(rbac_graph.physician)],
        )
        cursor.execute(
            "SELECT principal_id FROM clinic_app.enterprise_scim_audit_scope(%s)",
            [scope.clinic_id],
        )
        assert cursor.fetchall() == []
