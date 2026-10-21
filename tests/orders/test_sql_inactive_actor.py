"""Direct actor-GUC probes execute the source and machine authority protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from apps.orders.services import ingest_result
from apps.tenancy.envelope import _kek
from django.db import ProgrammingError, connection, transaction

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from identity.test_lifecycle_sql_readers import _context
from orders.support import laboratory, ordered, submission

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_staff_result_protocol_requires_current_active_actor(
    rbac_graph: RbacGraph,
) -> None:
    # Given a matched source and its currently authorized ordering clinician.
    graph = rbac_graph
    world = ordered(graph)
    with as_actor(graph, graph.physician):
        receipt = ingest_result(clinic_id=graph.clinic_a, submission=submission(world))
    decisions = []

    # When only the stored actor's active flag changes, bypass Python admission.
    for active in (True, False, True):
        with owner_context(graph.organization_a):
            assert User.objects.filter(pk=graph.physician).update(is_active=active) == 1
        with (
            _context(actor=graph.physician, tenant=graph.organization_a),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.orders_can_read_version(%s)", [receipt.version_id]
            )
            decisions.append(cursor.fetchone()[0])
            if active:
                cursor.execute(
                    "SELECT state FROM clinic_app.orders_verify_identity(%s,%s,%s,%s)",
                    [graph.clinic_a, receipt.version_id, world.order.pk, _kek()],
                )
                assert cursor.fetchone() == ("matched",)
            else:
                with pytest.raises(ProgrammingError) as denied, transaction.atomic():
                    cursor.execute(
                        "SELECT state FROM "
                        "clinic_app.orders_verify_identity(%s,%s,%s,%s)",
                        [graph.clinic_a, receipt.version_id, world.order.pk, _kek()],
                    )
                assert getattr(denied.value.__cause__, "sqlstate", None) == "42501"

    # Then both the read gate and matching protocol refuse the inactive actor.
    assert decisions == [True, False, True]


def test_raw_orders_guard_refuses_inactive_actor(rbac_graph: RbacGraph) -> None:
    # Given a placed order whose next two legitimate transitions are known.
    graph = rbac_graph
    world = ordered(graph)
    with owner_context(graph.organization_a):
        sequence = world.order.events.count() + 1
    accepted = []

    # When direct runtime-role INSERTs bind the actor without tenant_context.
    for active, state in (
        (True, "collected"),
        (False, "in_process"),
        (True, "in_process"),
    ):
        with owner_context(graph.organization_a):
            assert User.objects.filter(pk=graph.physician).update(is_active=active) == 1
        with (
            _context(actor=graph.physician, tenant=graph.organization_a),
            connection.cursor() as cursor,
        ):
            statement = (
                "INSERT INTO clinic_app.orders_orderevent "
                "(id,organization_id,clinic_id,created_at,order_id,sequence,"
                "state,actor_id,command_id) VALUES "
                "(%s,%s,%s,statement_timestamp(),%s,%s,%s,%s,%s)"
            )
            values: list[UUID | int | str] = [
                uuid4(),
                graph.organization_a,
                graph.clinic_a,
                world.order.pk,
                sequence,
                state,
                graph.physician,
                uuid4(),
            ]
            if active:
                cursor.execute(statement, values)
                accepted.append(state)
                sequence += 1
            else:
                with pytest.raises(ProgrammingError) as denied, transaction.atomic():
                    cursor.execute(statement, values)
                cause = denied.value.__cause__
                assert isinstance(cause, psycopg.Error)
                assert cause.sqlstate == "42501"
                assert "orders_guard() line" in (cause.diag.context or "")

    # Then the actual guard, not an earlier Python or RLS rejection, refused.
    assert accepted == ["collected", "in_process"]


def test_lab_sql_requires_absent_staff_actor(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    # Given a genuine, independently authenticated synthetic lab account.
    graph = rbac_graph
    lab = laboratory(graph, settings)
    decisions = []

    # When the same valid lab proof also carries an active or inactive actor.
    for actor, active in (
        (None, True),
        (graph.physician, True),
        (graph.physician, False),
        (None, True),
    ):
        with owner_context(graph.organization_a):
            assert User.objects.filter(pk=graph.physician).update(is_active=active) == 1
        with (
            _context(actor=actor, tenant=graph.organization_a),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT count(*) FROM clinic_app.orders_resolve_lab(%s,%s)",
                [lab.account, lab.key],
            )
            resolved = cursor.fetchone()[0]
            cursor.execute(
                "SELECT set_config('app.current_principal',%s,true),"
                "set_config('app.orders_lab_proof',%s,true)",
                [str(lab.principal), lab.key],
            )
            cursor.execute("SELECT clinic_app.orders_lab_account(%s)", [graph.clinic_a])
            decisions.append((resolved, cursor.fetchone()[0] == lab.account))

    # Then neither machine-only reader ever accepts a non-empty staff actor.
    assert decisions == [(1, True), (0, False), (0, False), (1, True)]
