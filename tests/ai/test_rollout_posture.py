"""The existing tenant ledgers retain their ACLs and gain exact scope bindings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.migrations import _rollout_sql
from apps.ai.rls import ROLLOUT_TABLES
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ai.evaluation_support import as_reviewer, evaluation_world
from ai.test_rollout import approve_stage
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_internal_stage_reader_is_not_a_runtime_cross_tenant_reader(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    change = approve_stage(world)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
        connection.cursor() as cursor,
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        cursor.execute("SELECT clinic_app.ai_rollout_status(%s)", [change.pk])
    with as_reviewer(world, world.owner), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_rollout_status_for(%s,%s),"
            "clinic_app.ai_rollout_status_for(%s,%s)",
            [world.clinic, change.pk, rbac_graph.clinic_c, change.pk],
        )
        assert cursor.fetchone() == ("shadow", "invalidated")


def test_reverse_refuses_to_strip_retained_stage_evidence(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    approve_stage(world)
    with (
        owner_context(world.organization),
        pytest.raises(
            DatabaseError, match="retained rollout evidence requires restore"
        ),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(_rollout_sql.REVERSE_SQL)


def test_rollout_uses_existing_force_rls_ledgers_and_scoped_foreign_keys() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "c.relowner::regrole::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relname=ANY(%s)",
            [sorted(ROLLOUT_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in ROLLOUT_TABLES
        }
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname=ANY(%s)",
            [
                [
                    "ai_rollout_run_scope",
                    "ai_rollout_invocation_scope",
                    "ai_rollout_agent_scope",
                ]
            ],
        )
        constraints = dict(cursor.fetchall())
        assert set(constraints) == {
            "ai_rollout_run_scope",
            "ai_rollout_invocation_scope",
            "ai_rollout_agent_scope",
        }
        assert all(
            "FOREIGN KEY (organization_id, clinic_id, rollout_approval_id)"
            in definition
            and "ai_modelchangerequest(organization_id, clinic_id, id)" in definition
            for definition in constraints.values()
        )
