"""Exact catalog posture and raw SQL authority, independent of Python guards."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.adapters import register_provider
from apps.ai.artifacts import create_input
from apps.ai.contracts import Purpose
from apps.ai.models import AICircuit, AIInvocation
from apps.ai.owner import reset_circuit
from apps.ai.rls import (
    AGENT_GRANTS,
    AGENT_TABLES,
    COLUMN_GRANTS,
    CUSTOM_RLS_TABLES,
    EXTRACTION_TABLES,
    RUNTIME_GRANTS,
)
from apps.core.integration import execute_operation
from apps.identity.models import CareTeamMembership, User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ai.support import DEFAULT_BUDGET, SENTINEL, enqueue, seed
from ai.test_gateway import TimeoutProvider
from auth.stepup_test_support import create_role_actor
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_ai_tables_have_exact_policies_flags_and_runtime_privileges() -> None:
    # Given: the migrated app, with no product guard monkeypatches.
    tables = sorted(RUNTIME_GRANTS)
    with connection.cursor() as cursor:
        # When: PostgreSQL reports the actual flags, policies and ACLs.
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,relowner::regrole::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND relname=ANY(%s)",
            [tables],
        )
        flags = set(cursor.fetchall())
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [tables],
        )
        policies = set(cursor.fetchall())
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND table_name=ANY(%s)",
            [tables],
        )
        grants = set(cursor.fetchall())
        # Table UPDATE already appears in grants; inspect only explicit
        # column ACLs here, as the exact resolver-catalog test does.
        cursor.execute(
            """
            SELECT class.relname, attribute.attname, acl.privilege_type
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS class ON class.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'clinic_app'
              AND grantee.rolname = 'clinic_app' AND acl.privilege_type = 'UPDATE'
              AND class.relname = ANY(%s)
            """,
            [tables],
        )
        columns = set(cursor.fetchall())
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee='clinic_agent' "
            "AND table_name=ANY(%s)",
            [tables],
        )
        machine_grants = set(cursor.fetchall())
    # Then: no permissive tenant-only clinical policy or write grant is hidden.
    assert flags == {
        (table, table in CUSTOM_RLS_TABLES, table in CUSTOM_RLS_TABLES, "clinic_owner")
        for table in tables
    }
    assert policies == {
        ("ai_aiartifact", "ai_owner"),
        ("ai_aiartifact", "ai_artifact_user"),
        ("ai_aiinvocation", "ai_owner"),
        ("ai_aiinvocation", "ai_invocation_user"),
        ("ai_aicircuit", "ai_owner"),
        ("ai_aicircuit", "ai_circuit_tenant"),
        *(
            (table, policy)
            for table in AGENT_TABLES
            for policy in ("agent_owner", "agent_staff", "agent_machine")
        ),
        *(
            (table, policy)
            for table in EXTRACTION_TABLES
            for policy in ("extraction_owner", "extraction_staff")
        ),
    }
    assert grants == {
        (table, privilege)
        for table, privileges in RUNTIME_GRANTS.items()
        for privilege in privileges
    }
    assert columns == set(COLUMN_GRANTS)
    assert machine_grants == {
        (table, privilege)
        for table, privileges in AGENT_GRANTS.items()
        for privilege in privileges
    }


def test_ai_sql_role_and_scope_decisions_follow_exact_permissions(
    rbac_graph: RbacGraph,
) -> None:
    # Given: every stored role, including owner and org_admin.
    for role in UserClinicRole.Role:
        actor = create_role_actor(rbac_graph, role)
        with (
            runtime_role(),
            tenant_context(actor.pk, rbac_graph.organization_a),
            connection.cursor() as cursor,
        ):
            # When: the deployed SQL guard, not Python, decides admission.
            cursor.execute(
                "SELECT clinic_app.ai_staff(%s,'front_desk',NULL),"
                "clinic_app.ai_staff(%s,'analyst',NULL),"
                "clinic_app.ai_staff(%s,'front_desk',NULL),"
                "clinic_app.ai_staff(%s,'untrusted-purpose',NULL)",
                [
                    rbac_graph.clinic_a,
                    rbac_graph.clinic_a,
                    rbac_graph.clinic_c,
                    rbac_graph.clinic_a,
                ],
            )
            # Then: permission identity and clinic isolation both matter.
            assert cursor.fetchone() == (
                "appointment.read" in BUNDLES_V1[role],
                "charge.read" in BUNDLES_V1[role],
                False,
                False,
            )


def test_clinical_purposes_recheck_current_patient_authority(
    rbac_graph: RbacGraph,
) -> None:
    # Given: an independently registered professional and current care team.
    actor, enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, actor), connection.cursor() as cursor:
        # When: each clinical purpose uses the same patient-scoped SQL predicate.
        for purpose in (
            "scribe_asr",
            "scribe_draft",
            "brief",
            "extraction",
            "decision_support",
        ):
            cursor.execute(
                "SELECT clinic_app.ai_staff(%s,%s,%s)",
                [rbac_graph.clinic_a, purpose, enrollment],
            )
            assert cursor.fetchone() == (True,)
    with owner_context(rbac_graph.organization_a):
        CareTeamMembership.objects.filter(user_id=actor).update(
            revoked_at=timezone.now()
        )
    with permission_context(rbac_graph, actor), connection.cursor() as cursor:
        # Then: committed revocation blocks the next call.
        cursor.execute(
            "SELECT clinic_app.ai_staff(%s,'brief',%s)",
            [rbac_graph.clinic_a, enrollment],
        )
        assert cursor.fetchone() == (False,)


def test_raw_invocation_cannot_forge_a_cheaper_budget_reservation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: authorized source and reviewed nonzero prices.
    world = seed(rbac_graph, monkeypatch)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        source = create_input(
            clinic_id=rbac_graph.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
        )
        # When/Then: direct runtime SQL cannot understate the reservation.
        with pytest.raises(DatabaseError), transaction.atomic():
            AIInvocation.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                user_id=rbac_graph.shared_user,
                purpose="front_desk",
                configuration=world.configuration,
                config_version=world.configuration.version,
                prompt_sha256="a" * 64,
                inputs_ref=source,
                limit_tokens_in=DEFAULT_BUDGET.limits.tokens_in,
                limit_tokens_out=DEFAULT_BUDGET.limits.tokens_out,
                limit_audio_seconds=DEFAULT_BUDGET.limits.audio_seconds,
                reserved_micros=0,
            )
        assert not AIInvocation.objects.exists()


def test_ledger_guard_refuses_inactive_admission_with_raw_actor_context(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        inputs = [
            create_input(
                clinic_id=rbac_graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                content=SENTINEL,
            ).pk
            for _ in range(3)
        ]
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(a)::text FROM clinic_app.ai_aiartifact a WHERE id=%s",
            [inputs[0]],
        )
        (artifact_json,) = cursor.fetchone()
        artifact = json.loads(artifact_json)
        cursor.execute(
            "SELECT to_jsonb(i)::text FROM clinic_app.ai_aiinvocation i WHERE id=%s",
            [result.invocation_id],
        )
        (invocation_json,) = cursor.fetchone()
        invocation = json.loads(invocation_json)
    inactive_refusals: set[str] = set()
    for active, input_id in zip((True, False, True), inputs, strict=True):
        with owner_context(rbac_graph.organization_a):
            assert (
                User.objects.filter(pk=rbac_graph.shared_user).update(is_active=active)
                == 1
            )
        candidates = (
            (
                "INSERT INTO clinic_app.ai_aiartifact SELECT "
                "(jsonb_populate_record(NULL::clinic_app.ai_aiartifact,%s::jsonb)).*",
                {**artifact, "id": str(uuid4())},
                "AI artifact denied",
            ),
            (
                "INSERT INTO clinic_app.ai_aiinvocation SELECT "
                "(jsonb_populate_record(NULL::clinic_app.ai_aiinvocation,%s::jsonb)).*",
                {
                    **invocation,
                    "id": str(uuid4()),
                    "inputs_ref_id": str(input_id),
                    "operation_id": None,
                },
                "AI reservation denied",
            ),
        )
        for statement, candidate, refusal in candidates:
            with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_tenant',%s,true), "
                    "set_config('app.current_user_id',%s,true)",
                    [str(rbac_graph.organization_a), str(rbac_graph.shared_user)],
                )
                if active:
                    cursor.execute(statement, [json.dumps(candidate)])
                    assert cursor.rowcount == 1
                else:
                    with pytest.raises(DatabaseError) as denied, transaction.atomic():
                        cursor.execute(statement, [json.dumps(candidate)])
                    cause = denied.value.__cause__
                    assert isinstance(cause, psycopg.Error)
                    assert cause.sqlstate == "23514"
                    assert cause.diag.message_primary == refusal
                    assert "ai_ledger_guard()" in (cause.diag.context or "")
                    inactive_refusals.add(refusal)
    assert inactive_refusals == {"AI artifact denied", "AI reservation denied"}


def test_runtime_cannot_write_global_terms_and_owner_cannot_delete_artifacts(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: real owner-managed global rows and one protected source.
    seed(rbac_graph, monkeypatch)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        source = create_input(
            clinic_id=rbac_graph.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
        )
        # When/Then: runtime ACLs and owner-side row triggers independently refuse.
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as c,
        ):
            c.execute("UPDATE clinic_app.ai_gatewayswitch SET enabled=false")
    with (
        owner_context(rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as c,
    ):
        c.execute("DELETE FROM clinic_app.ai_aiartifact WHERE id=%s", [source.pk])


def test_closed_circuit_can_reopen_without_losing_history(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a timed-out configuration whose first circuit an owner closed.
    world = seed(rbac_graph, monkeypatch)
    register_provider(TimeoutProvider())
    first = enqueue(world)
    assert first.operation_id is not None
    with runtime_role():
        assert execute_operation(first.operation_id) == "failed"
    with owner_context(rbac_graph.organization_a):
        assert (
            reset_circuit(
                clinic_id=rbac_graph.clinic_a, configuration_id=world.configuration.pk
            )
            == 1
        )
    # When: a newly admitted call times out after the reset.
    second = enqueue(world)
    assert second.operation_id is not None
    with runtime_role():
        assert execute_operation(second.operation_id) == "failed"
    # Then: closed history remains and exactly one fresh circuit is open.
    with owner_context(rbac_graph.organization_a):
        circuits = AICircuit.objects.filter(configuration=world.configuration)
        assert circuits.filter(closed_at__isnull=False).count() == 1
        assert circuits.filter(closed_at__isnull=True).count() == 1
