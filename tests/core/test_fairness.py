"""Per-tenant queue fairness: topology, quotas, bucket and failure posture.

The token-bucket cases run against a task-owned ``redis-server`` under the
same ``CLINIC_BROKER_GATE=required`` gate as the broker delivery suite;
validation, routing and failure-posture cases run everywhere.
"""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.core import fairness
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.models import ClinicConfiguration, User, UserClinicRole
from apps.tenancy.db import tenant_context
from config.celery import app as celery_app
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from redis import RedisError

from patient_service_support import runtime_role
from renewal.test_broker_delivery import _Broker, _free_port

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

GATED = pytest.mark.skipif(
    os.environ.get("CLINIC_BROKER_GATE", "") != "required",
    reason="requires the dedicated broker gate (CLINIC_BROKER_GATE=required)",
)

CONTENT: dict[str, Any] = {
    "display_name": "Clínica Sintética",
    "contact_email": "contato@example.invalid",
    "contact_phone": "+5511999990000",
    "brand_token": "teal",
    "reminder_hours": 12,
}


def _admin(graph: RbacGraph, clinic_id: UUID, organization_id: UUID) -> User:
    """Create one clinic admin bound to the given clinic and organization."""
    user = User.objects.create(
        username=f"todo9-admin-{uuid4().hex}",
        password=make_password("synthetic-password"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        UserClinicRole.objects.create(
            user_id=user.pk,
            organization_id=organization_id,
            clinic_id=clinic_id,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    return user


def _publish(
    graph: RbacGraph,
    clinic_id: UUID,
    organization_id: UUID,
    *,
    expected_version: int = 0,
    queue_quotas: dict[str, int] | None = None,
) -> ClinicConfiguration:
    actor = _admin(graph, clinic_id, organization_id)
    with runtime_role(), tenant_context(actor.pk, organization_id):
        return publish_configuration(
            clinic_id=clinic_id,
            expected_version=expected_version,
            content=ConfigurationContent(queue_quotas=queue_quotas, **CONTENT),
        )


@pytest.fixture(scope="session")
def fairness_broker(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_Broker]:
    """Run one task-owned Redis instance for the token-bucket cases."""
    binary = shutil.which("redis-server")
    if binary is None:
        if os.environ.get("CLINIC_BROKER_GATE") == "required":
            pytest.fail("broker gate requires the redis-server binary")
        pytest.skip("redis-server is not installed")
    handle = _Broker(
        binary=binary,
        root=tmp_path_factory.mktemp("fairness-redis"),
        port=_free_port(),
    )
    handle.start()
    yield handle
    handle.stop()


@pytest.fixture
def metered_broker(fairness_broker: _Broker, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point fairness metering at the task-owned broker."""
    monkeypatch.setattr(fairness, "_broker_url", lambda: fairness_broker.url)
    return fairness_broker.url


def test_queue_topology_matches_declared_queues() -> None:
    declared = {queue.name for queue in celery_app.conf.task_queues}
    assert declared == set(fairness.QUEUE_NAMES)
    assert celery_app.conf.task_default_queue == "clinic-integrations"


def test_route_table_maps_task_modules_to_queues() -> None:
    routes = celery_app.conf.task_routes
    assert routes["comms.*"]["queue"] == "clinic-integrations"
    module_queues = {
        "apps.ehr.tasks.*": "clinical",
        "apps.prescription.tasks.*": "clinical",
        "apps.scribe.tasks.*": "ai-interactive",
        "apps.ai.tasks.*": "ai-batch",
        "apps.comms.tasks.*": "messaging",
        "apps.billing.tasks.*": "finance",
        "apps.insurance.tasks.*": "finance",
        "apps.subscriptions.tasks.*": "finance",
        "apps.retention.tasks.*": "bulk",
        "apps.workflows.tasks.*": "bulk",
        "apps.analytics.tasks.*": "bulk",
        "apps.crm.tasks.*": "bulk",
    }
    for pattern, queue in module_queues.items():
        assert routes[pattern]["queue"] == queue
    routed = celery_app.amqp.router.route({}, "comms.execute_operation")
    assert routed["queue"].name == "clinic-integrations"
    routed = celery_app.amqp.router.route({}, "apps.ai.tasks.draft")
    assert routed["queue"].name == "ai-batch"


def test_beat_schedule_is_unchanged() -> None:
    assert celery_app.conf.beat_schedule == {
        "appointment-reminders": {
            "task": "comms.dispatch_due_reminders",
            "schedule": 60.0,
        },
        "pending-operation-recovery": {
            "task": "comms.recover_pending_operations",
            "schedule": 60.0,
        },
    }


@pytest.mark.parametrize(
    "quotas",
    [
        {"ai-batch": 0},
        {"ai-batch": -5},
        {"ai-batch": fairness.MAX_QUOTA_PER_MINUTE + 1},
        {"ai-batch": 1.5},
        {"ai-batch": True},
        {"ai-batch": "60"},
        {"forged-queue": 60},
        {"ai-batch": 60, "clinical": 0},
        [("ai-batch", 60)],
        "ai-batch",
        60,
    ],
)
def test_validate_queue_quotas_rejects_forged_or_malformed_maps(
    quotas: object,
) -> None:
    with pytest.raises(ValidationError):
        fairness.validate_queue_quotas(quotas)


def test_validate_queue_quotas_accepts_bounded_known_queues() -> None:
    assert fairness.validate_queue_quotas({}) == {}
    assert fairness.validate_queue_quotas(
        {"ai-batch": 60, "bulk": 1, "clinical": fairness.MAX_QUOTA_PER_MINUTE}
    ) == {"ai-batch": 60, "bulk": 1, "clinical": fairness.MAX_QUOTA_PER_MINUTE}


def test_fair_acquire_rejects_malformed_input() -> None:
    with pytest.raises(fairness.FairnessInputError):
        fairness.fair_acquire(organization_id="not-a-uuid", queue="bulk")  # type: ignore[arg-type]
    with pytest.raises(fairness.FairnessInputError):
        fairness.fair_acquire(organization_id=uuid4(), queue="forged-queue")
    with pytest.raises(fairness.FairnessInputError):
        fairness.fair_acquire(organization_id=uuid4(), queue="")


def test_publish_configuration_stores_and_carries_quotas(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    first = _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        queue_quotas={"ai-batch": 60, "bulk": 120},
    )
    assert first.queue_quotas == {"ai-batch": 60, "bulk": 120}
    second = _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        expected_version=1,
    )
    assert second.queue_quotas == {"ai-batch": 60, "bulk": 120}
    third = _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        expected_version=2,
        queue_quotas={"ai-batch": 30},
    )
    assert third.queue_quotas == {"ai-batch": 30}


def test_forged_quota_rejected_by_service_and_database(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = _admin(graph, graph.clinic_a, graph.organization_a)
    with runtime_role(), tenant_context(actor.pk, graph.organization_a):
        with pytest.raises(ValidationError):
            publish_configuration(
                clinic_id=graph.clinic_a,
                expected_version=0,
                content=ConfigurationContent(
                    queue_quotas={"ai-batch": fairness.MAX_QUOTA_PER_MINUTE + 1},
                    **CONTENT,
                ),
            )
        with pytest.raises(ValidationError):
            publish_configuration(
                clinic_id=graph.clinic_a,
                expected_version=0,
                content=ConfigurationContent(
                    queue_quotas={"unknown-queue": 10},
                    **CONTENT,
                ),
            )
        assert not ClinicConfiguration.objects.exists()
        for forged in (
            {"ai-batch": 0},
            {"ai-batch": fairness.MAX_QUOTA_PER_MINUTE + 1},
            {"ai-batch": 1.5},
            {"unknown-queue": 10},
            {"ai-batch": "60"},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                ClinicConfiguration.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    version=1,
                    display_name="Safe",
                    queue_quotas=forged,
                    published_by_id=actor.pk,
                )
        assert not ClinicConfiguration.objects.exists()


def test_resolver_returns_latest_org_quotas(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        queue_quotas={"ai-batch": 60},
    )
    _publish(
        graph,
        graph.clinic_b,
        graph.organization_a,
        queue_quotas={"ai-batch": 90},
    )
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 90}
        assert fairness._organization_quotas(graph.organization_b) == {}
        assert fairness._organization_quotas(uuid4()) == {}


def test_acquire_or_defer_reenqueues_with_countdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fairness, "fair_acquire", lambda *, organization_id, queue: False
    )
    calls: list[dict[str, object]] = []
    task = SimpleNamespace(
        request=SimpleNamespace(args=("a",), kwargs={"k": 1}),
        apply_async=lambda **options: calls.append(options),
    )
    assert (
        fairness.acquire_or_defer(task, organization_id=uuid4(), queue="bulk") is False
    )
    assert calls == [
        {
            "args": ("a",),
            "kwargs": {"k": 1},
            "countdown": fairness.DEFER_COUNTDOWN_SECONDS,
        }
    ]
    monkeypatch.setattr(
        fairness, "fair_acquire", lambda *, organization_id, queue: True
    )
    assert (
        fairness.acquire_or_defer(task, organization_id=uuid4(), queue="bulk") is True
    )
    assert len(calls) == 1


def test_deferral_emits_metric_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        fairness,
        "_emit_metric",
        lambda name, *, queue, reason: emitted.append((name, queue, reason)),
    )
    monkeypatch.setattr(
        fairness, "_broker_url", lambda: f"redis://127.0.0.1:{_free_port()}/0"
    )
    with runtime_role():
        assert fairness.fair_acquire(organization_id=uuid4(), queue="bulk") is False
        assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is True
    assert emitted == [
        ("clinic_fairness.deferred", "bulk", "redis_unavailable"),
        ("clinic_fairness.fail_open", "clinical", "redis_unavailable"),
    ]


def test_redis_outage_defers_metered_queues_and_fails_clinical_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead metering backend defers every queue except clinical."""
    monkeypatch.setattr(
        fairness, "_broker_url", lambda: f"redis://127.0.0.1:{_free_port()}/0"
    )
    with runtime_role():
        for queue in fairness.QUEUE_NAMES:
            acquired = fairness.fair_acquire(organization_id=uuid4(), queue=queue)
            # Only clinical may proceed when metering is unreachable.
            assert acquired is (queue == "clinical")


def test_non_redis_broker_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fairness, "_broker_url", lambda: "memory://")
    with runtime_role():
        assert fairness.fair_acquire(organization_id=uuid4(), queue="bulk") is False
        assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is True


def test_client_error_posture_is_redis_error() -> None:
    assert issubclass(fairness.FairnessBackendError, RedisError)


@GATED
def test_token_bucket_enforces_per_org_quota(
    rbac_graph: RbacGraph, metered_broker: str
) -> None:
    """Two organizations share one broker but never share a bucket."""
    graph = rbac_graph
    _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        queue_quotas={"ai-batch": 60},
    )
    _publish(
        graph,
        graph.clinic_c,
        graph.organization_b,
        queue_quotas={"ai-batch": 120},
    )
    with runtime_role():
        allowed_a = sum(
            fairness.fair_acquire(
                organization_id=graph.organization_a, queue="ai-batch"
            )
            for _ in range(200)
        )
        allowed_b = sum(
            fairness.fair_acquire(
                organization_id=graph.organization_b, queue="ai-batch"
            )
            for _ in range(200)
        )
        # A different queue for org A draws on its own bucket.
        allowed_a_bulk = sum(
            fairness.fair_acquire(organization_id=graph.organization_a, queue="bulk")
            for _ in range(200)
        )
    assert allowed_a == 60
    assert allowed_b == 120
    # The default-quota bulk bucket is separate and admits all 200.
    assert allowed_a_bulk == 200


@GATED
def test_token_bucket_refills_at_quota_rate(
    rbac_graph: RbacGraph,
    metered_broker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refill is driven by the injected clock, never by sleeping."""
    graph = rbac_graph
    _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        queue_quotas={"ai-batch": 60},
    )
    clock = 1_700_000_000.0
    monkeypatch.setattr(fairness, "_now", lambda: clock)
    with runtime_role():
        assert all(
            fairness.fair_acquire(
                organization_id=graph.organization_a, queue="ai-batch"
            )
            for _ in range(60)
        )
        assert not fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
        clock += 30.0  # half a minute -> 30 tokens at 60/minute
        assert (
            sum(
                fairness.fair_acquire(
                    organization_id=graph.organization_a, queue="ai-batch"
                )
                for _ in range(60)
            )
            == 30
        )


@GATED
def test_concurrent_acquires_never_exceed_quota(
    rbac_graph: RbacGraph, metered_broker: str
) -> None:
    """A Barrier race on one bucket admits exactly the quota."""
    graph = rbac_graph
    _publish(
        graph,
        graph.clinic_a,
        graph.organization_a,
        queue_quotas={"ai-batch": 60},
    )
    barrier = threading.Barrier(8, timeout=30)
    results: list[bool] = []
    lock = threading.Lock()

    def acquire() -> None:
        try:
            with runtime_role():
                barrier.wait()
                outcome = fairness.fair_acquire(
                    organization_id=graph.organization_a, queue="ai-batch"
                )
            with lock:
                results.append(outcome)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(acquire) for _ in range(200)]
        for future in futures:
            future.result(timeout=60)
    assert len(results) == 200
    assert sum(results) == 60
