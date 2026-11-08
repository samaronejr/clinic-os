"""Every workflow SQL protocol member has an executed, catalog-wide role oracle."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.workflows import engine, run_services
from apps.workflows.care.models import Recall
from apps.workflows.models import WorkflowDefinitionVersion, WorkflowStep
from apps.workflows.urgent_models import UrgentCoveragePolicy
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg import sql
from psycopg.types.json import Jsonb

from identity.legacy_sql_inventory import discover_sql
from identity.permission_support import permission_context
from workflows.care_world import seed_care_world
from workflows.guard_cases import GuardWorld, seed_guard_world
from workflows.test_authority import _set_role

if TYPE_CHECKING:
    from collections.abc import Callable

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
    # The care world's overdue action is an ordinary follow-up task, due just
    # after the generic one: the escalation scan lists both, oldest first.
    assert call("workflows_due_tasks", [w.run.created_at]) == [
        (w.due.pk,),
        (w.care.overdue_task,),
    ]
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
            created_by_id=w.actor,
            key="sql-guard-" + uuid4().hex,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
            creation_fingerprint="0" * 64,
            version=1,
            steps=[{"handler": "timer", "seconds": 0}],
        )
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_app")


def _clock(w: GuardWorld, permitted: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_now(), pg_catalog.statement_timestamp()"
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == row[1]


def _predecessor(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_predecessor_ready", [w.clinic, w.dependent.pk]) == [
        (permitted,),
    ]
    assert call("workflows_predecessor_ready", [w.graph.clinic_b, w.dependent.pk]) == [
        (False,),
    ]


def _care_guard(w: GuardWorld, permitted: bool) -> None:
    # Owner fixture access gets past RLS to execute the private trigger; its
    # tasks.reassign decision for a new recall remains entirely real.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
    try:
        now = timezone.now()
        Recall.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            enrollment_id=w.enrollment,
            reason="routine_return",
            interval_days=30,
            anchor_at=now,
            due_at=now + timedelta(days=30),
            owner_role="receptionist",
            created_by_id=w.actor,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
        )
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_app")


def _care_items(w: GuardWorld, permitted: bool) -> None:
    rows = call("workflows_care_items", [timezone.now(), w.care.recall], private=True)
    assert [row[:5] for row in rows] == [
        ("recall", w.care.recall, w.graph.organization_a, w.clinic, w.actor)
    ]
    assert call("workflows_care_items", [timezone.now(), uuid4()], private=True) == []


def _care_due(w: GuardWorld, permitted: bool) -> None:
    rows = call("workflows_care_due", [timezone.now()])
    # Opaque (kind, record) pairs only: no tenant or user id leaves (N7).
    assert ("recall", w.care.recall) in rows
    assert {len(row) for row in rows} == {2}


def _care_scope(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_care_scope", ["recall", w.care.recall]) == [
        (w.graph.organization_a, w.clinic, w.actor)
    ]
    assert call("workflows_care_scope", ["overdue", w.care.recall]) == []


def _care_park(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_care_park", ["recall", w.care.recall]) == [(True,)]
    assert call("workflows_care_park", ["recall", uuid4()]) == [(False,)]


def _care_version_scope(w: GuardWorld, permitted: bool) -> None:
    rows = call("workflows_care_version_scope", [w.clinic, w.care.new_version])
    assert len(rows) == (1 if permitted else 0)
    assert (
        call("workflows_care_version_scope", [w.graph.clinic_b, w.care.new_version])
        == []
    )


def _care_occurrence_scope(w: GuardWorld, permitted: bool) -> None:
    (row,) = call("workflows_care_occurrence_scope", [w.clinic, w.care.occurrence])
    assert (row[0] is not None) is permitted
    assert call(
        "workflows_care_occurrence_scope", [w.graph.clinic_b, w.care.occurrence]
    ) == [(None,)]


def _care_source_current(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_care_source_current", [w.care.window]) == [(True,)]
    assert call("workflows_care_source_current", [w.care.flagged]) == [(False,)]


def _care_system_task(w: GuardWorld, permitted: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        # As text: the connection's jsonb loader returns undecoded JSON.
        cursor.execute(
            "SELECT to_jsonb(t)::text FROM clinic_app.workflows_task t WHERE t.id=%s",
            [w.care.response_task],
        )
        stored = cursor.fetchone()
        cursor.execute("SET LOCAL ROLE clinic_app")
    assert stored is not None
    task = json.loads(stored[0])
    valid = "workflows_care_system_task_valid"
    assert call(valid, [Jsonb(task)], private=True) == [(True,)]
    assert call(valid, [Jsonb({**task, "priority": "low"})], private=True) == [(False,)]


def _granted(w: GuardWorld, permission: str) -> bool:
    [(allowed,)] = call("has_permission", [permission, w.clinic, None])
    return bool(allowed)


def _refused(sqlstate: str, invoke: Callable[[], object]) -> None:
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        invoke()
    assert getattr(caught.value.__cause__, "sqlstate", None) == sqlstate


def _urgent_responsible(w: GuardWorld, permitted: bool) -> None:
    # A responsible actor holds one of the named roles and demographics.read.
    roles = list(UserClinicRole.Role.values)
    gate = _granted(w, "demographics.read")
    responsible = "workflows_urgent_responsible"
    assert call(responsible, [w.clinic, roles]) == [(permitted and gate,)]
    assert call(responsible, [w.clinic, []]) == [(False,)]
    assert call(responsible, [w.graph.clinic_b, roles]) == [(False,)]


def _coverage_policy(w: GuardWorld, permitted: bool) -> None:
    # Resolver-only lookup of the latest policy: no actor, no role decision.
    organization = w.graph.organization_a
    for clinic in (w.clinic, w.graph.clinic_b, uuid4()):
        rows = call("workflows_coverage_policy", [organization, clinic], private=True)
        assert rows == [(None,)]


def _report_urgent(w: GuardWorld, permitted: bool) -> None:
    # Only a live patient session reaches the bridge; staff never do.
    _refused("42501", lambda: call("workflows_report_urgent", [uuid4()]))


def _patient_coverage(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_patient_coverage", []) == []


def _urgent_audit_scope(w: GuardWorld, permitted: bool) -> None:
    assert call("workflows_urgent_audit_scope", [uuid4()], private=True) == []


def _urgent_audit(w: GuardWorld, permitted: bool) -> None:
    _refused(
        "42501",
        lambda: call("workflows_urgent_audit", [uuid4(), timezone.now(), bytes(32)]),
    )


def _release_send_eligible(w: GuardWorld, permitted: bool) -> None:
    # Only a known, now-ineligible release notice is held back.
    assert call("workflows_release_send_eligible", [uuid4()]) == [(True,)]


def _release_system_task(w: GuardWorld, permitted: bool) -> None:
    # Without the outcome bridge's failure marker no task row is admitted.
    valid = "workflows_release_system_task_valid"
    row = {"id": str(uuid4()), "kind": "follow_up"}
    assert call(valid, [Jsonb(row)], private=True) == [(False,)]


def _release_outcome(w: GuardWorld, permitted: bool) -> None:
    # The outcome function reads no actor, only the tenant; its effect is pinned
    # by test_urgent_queue.test_failed_notice_delivery_opens_one_follow_up_task.
    # Every role sees the one binding: terminal transitions of message sends.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_get_triggerdef(t.oid) FROM pg_catalog.pg_trigger t "
            "WHERE t.tgfoid='clinic_app.workflows_release_outcome()'::regprocedure "
            "AND NOT t.tgisinternal"
        )
        [(definition,)] = cursor.fetchall()
    assert "AFTER UPDATE OF status ON clinic_app.comms_integrationoperation" in (
        definition
    )
    assert "'comms.message'" in definition


def _coordination_guard(w: GuardWorld, permitted: bool) -> None:
    # A coverage version needs configuration.clinic and the acting publisher.
    allowed = permitted and _granted(w, "configuration.clinic")

    def publish() -> None:
        UrgentCoveragePolicy.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            version=1,
            weekdays=[0, 1, 2, 3, 4],
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            response_hours=4,
            responsible_roles=["nurse"],
            created_by_id=w.actor,
        )

    if allowed:
        with transaction.atomic():
            publish()
    else:
        _refused("42501", publish)


def _receipt_refused(step: WorkflowStep) -> None:
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        WorkflowStep.objects.filter(pk=step.pk).update(
            state="completed", claim_until=None
        )
    assert getattr(caught.value.__cause__, "sqlstate", None) == "23514"
    assert "workflow effect is missing" in str(caught.value.__cause__)


def _seed_metric_step(w: GuardWorld) -> WorkflowStep:
    """Claim one metrics_refresh step as the seeding clinic manager."""
    with permission_context(w.graph, w.actor):
        definition = run_services.publish_definition(
            clinic_id=w.clinic,
            key="sql-authority-metric-receipt",
            steps=[{"handler": "metrics_refresh", "metric_id": "no_show", "days": 30}],
            idempotency_key=uuid4(),
        )
        run = run_services.start_run(
            clinic_id=w.clinic,
            definition_version_id=definition.pk,
            context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
        assert engine.claim_step(step_id=step.pk) == 1
        # The manager reaches the step, so the per-role case is never vacuous.
        _receipt_refused(step)
    return step


def _metric_receipt(w: GuardWorld, permitted: bool) -> None:
    # No role completes a metrics_refresh step without its refresh receipt: a
    # role that reaches the step meets the receipt trigger before the staff
    # guard, any other updates no row. The receipt binding itself is pinned by
    # test_sql_decisions.metric_receipt_authority.
    steps = WorkflowStep.objects.filter(pk=w.metric.pk)
    if steps.exists():
        _receipt_refused(w.metric)
    else:
        assert steps.update(state="completed", claim_until=None) == 0


SQL_CASES = {
    "clinic_app.workflows_now": _clock,
    "clinic_app.workflows_predecessor_ready": _predecessor,
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
    "clinic_app.workflows_care_guard": _care_guard,
    "clinic_app.workflows_care_items": _care_items,
    "clinic_app.workflows_care_due": _care_due,
    "clinic_app.workflows_care_scope": _care_scope,
    "clinic_app.workflows_care_park": _care_park,
    "clinic_app.workflows_care_version_scope": _care_version_scope,
    "clinic_app.workflows_care_occurrence_scope": _care_occurrence_scope,
    "clinic_app.workflows_care_source_current": _care_source_current,
    "clinic_app.workflows_care_system_task_valid": _care_system_task,
    "clinic_app.workflows_urgent_responsible": _urgent_responsible,
    "clinic_app.workflows_coverage_policy": _coverage_policy,
    "clinic_app.workflows_report_urgent": _report_urgent,
    "clinic_app.workflows_patient_coverage": _patient_coverage,
    "clinic_app.workflows_urgent_audit_scope": _urgent_audit_scope,
    "clinic_app.workflows_urgent_audit": _urgent_audit,
    "clinic_app.workflows_release_send_eligible": _release_send_eligible,
    "clinic_app.workflows_release_system_task_valid": _release_system_task,
    "clinic_app.workflows_release_outcome": _release_outcome,
    "clinic_app.workflows_coordination_guard": _coordination_guard,
    "clinic_app.analytics_workflow_receipt": _metric_receipt,
}
# Trigger guards: only a manager's own write passes their staff decision.
GUARDS = {"clinic_app.workflows_guard", "clinic_app.workflows_care_guard"}


def test_every_workflow_sql_member_for_every_catalog_role(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(SQL_CASES) == workflow_sql_members()
    world = seed_guard_world(rbac_graph, monkeypatch)
    world = replace(world, care=seed_care_world(world))
    world = replace(world, metric=_seed_metric_step(world))
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
                if name in GUARDS and role not in managers:
                    with pytest.raises(DatabaseError) as caught, transaction.atomic():
                        invoke(world, member)
                    assert getattr(caught.value.__cause__, "sqlstate", None) == "23514"
                else:
                    invoke(world, member)
                transaction.set_rollback(True)
