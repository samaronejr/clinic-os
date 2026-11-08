from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.artifacts import create_input
from apps.ai.contracts import Purpose
from apps.ai.models import AIInvocation
from apps.ai.services import invoke
from apps.subscriptions.models import UsageEvent, UsageRollup
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction

from ai.support import DEFAULT_BUDGET, SENTINEL, enqueue, seed
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from subscriptions.support import publish_quota

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.contracts import InvocationResult

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_last_ai_allowance_admits_one_reservation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    world = seed(graph, monkeypatch)
    publish_quota(graph, world.administrator, "ai_tokens_in", 100)
    with permission_context(graph, graph.shared_user):
        sources = [
            create_input(
                clinic_id=graph.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
            ).pk
            for _ in range(2)
        ]
    ready = Barrier(2, timeout=20)

    def attempt(source: UUID) -> InvocationResult:
        connections.close_all()
        try:
            ready.wait()
            with (
                runtime_role(),
                tenant_context(graph.shared_user, graph.organization_a),
            ):
                return invoke(
                    clinic_id=graph.clinic_a,
                    purpose=Purpose.FRONT_DESK,
                    principal=graph.shared_user,
                    inputs_ref=source,
                    budget=DEFAULT_BUDGET,
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, sources))
    assert sum(result.invocation_id is not None for result in results) == 1
    assert sum(result.reason == "budget_exhausted" for result in results) == 1
    with owner_context(graph.organization_a):
        assert (
            AIInvocation.objects.filter(organization_id=graph.organization_a).count()
            == 1
        )


def test_raw_ai_insert_cannot_bypass_the_subscription_quota(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    world = seed(graph, monkeypatch)
    publish_quota(graph, world.administrator, "ai_tokens_in", 100)
    first = enqueue(world)
    assert first.invocation_id is not None
    with permission_context(graph, graph.shared_user):
        source = create_input(
            clinic_id=graph.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
        )
        forged = AIInvocation.objects.get(pk=first.invocation_id)
        forged.pk = uuid4()
        forged.inputs_ref = source
        forged.operation_id = None
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            forged.save(force_insert=True)
        cause = refused.value.__cause__
        assert cause is not None
        assert getattr(cause, "sqlstate", None) == "23514"
        assert (
            getattr(getattr(cause, "diag", None), "message_primary", None)
            == "subscription allowance unavailable"
        )


def test_concurrent_equal_usage_fact_updates_rollup_once(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    source = uuid4()
    ready = Barrier(2, timeout=20)
    with connection.cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")
        row = cursor.fetchone()
    assert row is not None
    stamp = row[0]

    def record() -> None:
        connections.close_all()
        try:
            ready.wait()
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_resolver")
                cursor.execute(
                    "SELECT clinic_app.subscription_record_usage("
                    "%s,'messages',7,%s,%s)",
                    [graph.organization_a, stamp, source],
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(record)
        second = pool.submit(record)
        first.result()
        second.result()
    with owner_context(graph.organization_a):
        assert UsageEvent.objects.filter(source_ref=source).count() == 1
        assert (
            UsageRollup.objects.get(
                organization_id=graph.organization_a, meter="messages"
            ).quantity
            == 7
        )
