"""Every workflow SQL protocol member has an executed, catalog-wide role oracle."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.workflows.models import WorkflowDefinitionVersion
from django.db import DatabaseError, connection, transaction
from psycopg import sql
from psycopg.types.json import Jsonb

from identity.legacy_sql_inventory import discover_sql
from identity.permission_support import permission_context
from workflows.guard_cases import GuardWorld, seed_guard_world
from workflows.test_authority import _set_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def workflow_sql_members() -> set[str]:
    return {
        name
        for name, entry in discover_sql().items()
        if any(
            path.startswith("apps/workflows/migrations/")
            for path in entry["migrations"]
        )
    }


def call(
    name: str, arguments: list[Any], *, private: bool = False
) -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        if private:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
        try:
            cursor.execute(
                sql.SQL("SELECT * FROM clinic_app.{}({})").format(
                    sql.Identifier(name),
                    sql.SQL(",").join(sql.Placeholder() for _ in arguments),
                ),
                arguments,
            )
            return list(cursor.fetchall())
        finally:
            if private and not connection.needs_rollback:
                cursor.execute("SET LOCAL ROLE clinic_app")


def _owned(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_owned", [w.clinic, w.actor, ""]) == [(permitted,)]
    assert call("workflows_owned", [w.graph.clinic_b, w.actor, ""]) == [(False,)]


def _owner_valid(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_owner_valid", [w.clinic, w.actor, ""]) == [(permitted,)]
    assert call("workflows_owner_valid", [w.graph.clinic_b, w.actor, ""]) == [(False,)]


def _staff(w: GuardWorld, permitted: bool) -> None:
    rows = call("workflows_staff_catalog", [w.clinic])
    assert any(row[0] == w.actor for row in rows) is permitted
    assert call("workflows_staff_catalog", [w.graph.clinic_b]) == []


def _reference(w: GuardWorld, permitted: bool) -> None:
    assert call(
        "workflows_reference_valid",
        [
            Jsonb({"kind": "clinic", "id": str(w.clinic)}),
            w.graph.organization_a,
            w.clinic,
        ],
        private=True,
    ) == [(True,)]
    assert call(
        "workflows_reference_valid",
        [
            Jsonb({"kind": "clinic", "id": str(uuid4())}),
            w.graph.organization_a,
            w.clinic,
        ],
        private=True,
    ) == [(False,)]


def _steps(w: GuardWorld, permitted: bool) -> None:
    assert call(
        "workflows_steps_valid",
        [Jsonb([{"handler": "timer", "seconds": 0}])],
        private=True,
    ) == [(True,)]
    assert call(
        "workflows_steps_valid",
        [Jsonb([{"handler": "shell", "command": "SINTETICO-SENTINELA"}])],
        private=True,
    ) == [(False,)]


def _step_scope(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_step_scope", [w.pending.pk]) == [
        (w.graph.organization_a, w.clinic, w.actor)
    ]
    assert call("workflows_step_scope", [uuid4()]) == []


def _task_scope(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_task_scope", [w.open_task.pk]) == [
        (w.graph.organization_a, w.clinic, w.actor)
    ]
    assert call("workflows_task_scope", [uuid4()]) == []


def _due_steps(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_due_steps", [w.run.created_at]) == [
        (identifier,) for identifier in sorted((w.pending.pk, w.task_step.pk))
    ]


def _due_tasks(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_due_tasks", [w.run.created_at]) == [(w.due.pk,)]
    assert call("workflows_due_tasks", [w.due.due_at - timedelta(microseconds=1)]) == []


def _guard(w: GuardWorld, permitted: bool) -> None:
    # Owner fixture access deliberately gets past RLS to execute the private
    # trigger. Its role/active-account decision remains entirely real.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
    try:
        WorkflowDefinitionVersion.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            published_by_id=w.actor,
            key="sql-guard-" + uuid4().hex,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
            version=1,
            steps=[{"handler": "timer", "seconds": 0}],
        )
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_app")


SQL_CASES = {
    "clinic_app.workflows_owned": _owned,
    "clinic_app.workflows_owner_valid": _owner_valid,
    "clinic_app.workflows_staff_catalog": _staff,
    "clinic_app.workflows_reference_valid": _reference,
    "clinic_app.workflows_steps_valid": _steps,
    "clinic_app.workflows_step_scope": _step_scope,
    "clinic_app.workflows_task_scope": _task_scope,
    "clinic_app.workflows_due_steps": _due_steps,
    "clinic_app.workflows_due_tasks": _due_tasks,
    "clinic_app.workflows_guard": _guard,
}


def test_every_workflow_sql_member_for_every_catalog_role(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(SQL_CASES) == workflow_sql_members()
    world = seed_guard_world(rbac_graph, monkeypatch)
    managers = {"owner", "org_admin", "clinic_admin", "clinic_manager"}
    for role in (*UserClinicRole.Role.values, None, "inactive"):
        _set_role(
            world,
            "clinic_manager" if role == "inactive" else role,
            active=role != "inactive",
        )
        member = role is not None and role != "inactive"
        for name, invoke in SQL_CASES.items():
            with permission_context(rbac_graph, world.actor):
                if name == "clinic_app.workflows_guard" and role not in managers:
                    with pytest.raises(DatabaseError) as caught, transaction.atomic():
                        invoke(world, member)
                    assert getattr(caught.value.__cause__, "sqlstate", None) == "23514"
                else:
                    invoke(world, member)
                transaction.set_rollback(True)
