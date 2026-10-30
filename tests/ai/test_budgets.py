"""Real-connection reservation races and request rollback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.ai.artifacts import create_input
from apps.ai.contracts import Purpose
from apps.ai.models import AIInvocation
from apps.ai.services import invoke
from apps.tenancy.db import tenant_context
from django.db import connections, transaction

from ai.support import DEFAULT_BUDGET, SENTINEL, enqueue, seed, set_policy
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.ai.contracts import InvocationResult

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_reservations_cannot_overspend_one_organization(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one call fits, but two reservations exceed the organization cap.
    world = seed(rbac_graph, monkeypatch)
    set_policy(world, cap=1_010)
    barrier = Barrier(2, timeout=15)

    def reserve() -> InvocationResult:
        connections.close_all()
        try:
            barrier.wait()
            return enqueue(world)
        finally:
            connections.close_all()

    # When: independent runtime connections race the real advisory lock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: reserve(), range(2)))
    # Then: exactly one wins; no sleep or timing assumption is used.
    assert sum(result.invocation_id is not None for result in outcomes) == 1
    assert sum(result.reason == "budget_exhausted" for result in outcomes) == 1


def test_rollback_publishes_no_ai_job_or_reservation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: admission occurs in the request's transaction.
    world = seed(rbac_graph, monkeypatch)
    g = rbac_graph
    with runtime_role(), tenant_context(g.shared_user, g.organization_a):
        with transaction.atomic():
            source = create_input(
                clinic_id=g.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
            )
            invoke(
                clinic_id=g.clinic_a,
                purpose=Purpose.FRONT_DESK,
                principal=g.shared_user,
                inputs_ref=source.pk,
                budget=replace(DEFAULT_BUDGET, maximum_micros=20_000),
            )
            # When: the caller cancels before commit.
            transaction.set_rollback(True)
        # Then: neither reservation nor job survives.
        assert not AIInvocation.objects.exists()
    assert world.publications == []
