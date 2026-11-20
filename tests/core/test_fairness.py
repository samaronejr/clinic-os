"""Per-tenant queue fairness: topology, quotas, bucket and failure posture.

The token-bucket cases run against a task-owned ``redis-server`` under the
same ``CLINIC_BROKER_GATE=required`` gate as the broker delivery suite;
validation, routing, authority and failure-posture cases run everywhere.
Every exact admission count is taken under a frozen injected clock so no
assertion depends on execution speed.
"""

from __future__ import annotations

import logging
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import redis
from apps.core import fairness
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import ClinicConfiguration, User, UserClinicRole
from apps.tenancy.db import tenant_context
from config.celery import app as celery_app
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any

    from django.db.backends.utils import CursorWrapper

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

READY_MARKER = b"Ready to accept connections"
READY_TIMEOUT_SECONDS = 15.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _RedisBroker:
    """One task-owned Redis process on a unique loopback port.

    Readiness is event-driven: the server announces "Ready to accept
    connections" on stdout, so no polling sleep is ever needed.
    """

    def __init__(self, *, binary: str, root: Path, port: int) -> None:
        self.url = f"redis://127.0.0.1:{port}/0"
        self._argv = (
            binary,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
            "--protected-mode",
            "yes",
        )
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert self._process is None
        self._process = subprocess.Popen(  # noqa: S603 - fixed binary, closed argv
            self._argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        assert self._process.stdout is not None
        ready, _w, _x = select.select(
            [self._process.stdout], [], [], READY_TIMEOUT_SECONDS
        )
        if not ready:
            self.stop()
            pytest.fail("task-owned redis-server did not become ready")
        for line in self._process.stdout:
            if READY_MARKER in line:
                return
            if self._process.poll() is not None:
                break
        self.stop()
        pytest.fail("task-owned redis-server exited before readiness")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        if self._process.stdout is not None:
            self._process.stdout.close()
        self._process = None


@pytest.fixture(scope="session")
def fairness_broker(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_RedisBroker]:
    """Run one task-owned Redis instance for the token-bucket cases."""
    binary = shutil.which("redis-server")
    if binary is None:
        if os.environ.get("CLINIC_BROKER_GATE") == "required":
            pytest.fail("broker gate requires the redis-server binary")
        pytest.skip("redis-server is not installed")
    handle = _RedisBroker(
        binary=binary,
        root=tmp_path_factory.mktemp("fairness-redis"),
        port=_free_port(),
    )
    handle.start()
    yield handle
    handle.stop()


@pytest.fixture
def metered_broker(
    fairness_broker: _RedisBroker, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Point fairness metering at the task-owned broker."""
    monkeypatch.setattr(fairness, "_broker_url", lambda: fairness_broker.url)
    return fairness_broker.url


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Freeze the bucket clock; tests advance it explicitly."""
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(fairness, "_now", lambda: clock["t"])
    return clock


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


def _org_admin(graph: RbacGraph, organization_id: UUID) -> User:
    """Create one admin holding a role in every clinic of the organization."""
    user = User.objects.create(
        username=f"todo9-org-admin-{uuid4().hex}",
        password=make_password("synthetic-password"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        for clinic_id in (graph.clinic_a, graph.clinic_b):
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
    *,
    actor: User | None = None,
    expected_version: int = 0,
    queue_quotas: dict[str, int] | None = None,
) -> ClinicConfiguration:
    organization_id = (
        graph.organization_b if clinic_id == graph.clinic_c else graph.organization_a
    )
    if actor is None:
        actor = _admin(graph, clinic_id, organization_id)
    with runtime_role(), tenant_context(actor.pk, organization_id):
        return publish_configuration(
            clinic_id=clinic_id,
            expected_version=expected_version,
            content=ConfigurationContent(queue_quotas=queue_quotas, **CONTENT),
        )


def test_queue_topology_matches_declared_queues() -> None:
    declared = {queue.name for queue in celery_app.conf.task_queues}
    assert declared == set(fairness.QUEUE_NAMES)
    assert celery_app.conf.task_default_queue == "clinic-integrations"


def test_route_table_maps_task_modules_to_queues() -> None:
    routes = celery_app.conf.task_routes
    assert routes["comms.*"]["queue"] == "clinic-integrations"
    module_queues = {
        "apps.ehr.tasks.*": "clinical",
        "apps.scheduling.tasks.*": "clinical",
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
        "apps.interop.tasks.*": "bulk",
    }
    for pattern, queue in module_queues.items():
        assert routes[pattern]["queue"] == queue
    routed = celery_app.amqp.router.route({}, "comms.execute_operation")
    assert routed["queue"].name == "clinic-integrations"
    routed = celery_app.amqp.router.route({}, "apps.ai.tasks.draft")
    assert routed["queue"].name == "ai-batch"
    routed = celery_app.amqp.router.route({}, "apps.interop.tasks.commit_batch")
    assert routed["queue"].name == "bulk"
    routed = celery_app.amqp.router.route({}, "apps.scheduling.tasks.expire_holds")
    assert routed["queue"].name == "clinical"
    routed = celery_app.amqp.router.route({}, "apps.interop.tasks.expire_exports")
    assert routed["queue"].name == "bulk"


def test_beat_schedule_has_exact_durable_scanners() -> None:
    assert celery_app.conf.beat_schedule == {
        "appointment-reminders": {
            "task": "comms.dispatch_due_reminders",
            "schedule": 60.0,
        },
        "pending-operation-recovery": {
            "task": "comms.recover_pending_operations",
            "schedule": 60.0,
        },
        "workflow-due-scanner": {
            "task": "apps.workflows.tasks.scan_due",
            "schedule": 60.0,
        },
        "workflow-care-scanner": {
            "task": "apps.workflows.tasks.care_scan",
            "schedule": 60.0,
        },
        "retention-policy-sweep": {
            "task": "apps.retention.tasks.scan_due",
            "schedule": 60.0,
        },
        # Lifecycle v2 (D-9): the W hold-expiry job.
        "appointment-hold-expiry": {
            "task": "apps.scheduling.tasks.expire_holds",
            "schedule": 60.0,
        },
        "ehr-timeline-projection": {
            "task": "apps.ehr.tasks.refresh_timeline",
            "schedule": 60.0,
        },
        # Plan item 67: lost export hand-offs and archive expiry, bulk queue.
        "interop-export-dispatch": {
            "task": "apps.interop.tasks.dispatch_due_exports",
            "schedule": 300.0,
        },
        "interop-export-expiry": {
            "task": "apps.interop.tasks.expire_exports",
            "schedule": 900.0,
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
        {"ai-batch": [60]},
        {"ai-batch": None},
        {"ai-batch": {"nested": 1}},
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


def test_clinic_only_admin_cannot_set_organization_quotas(
    rbac_graph: RbacGraph,
) -> None:
    """A clinic-B-only admin must not raise the organization quota."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60},
    )
    clinic_b_only_admin = User.objects.get(pk=graph.clinic_admin)
    with (
        pytest.raises(CurrentActorError),
        runtime_role(),
        tenant_context(graph.clinic_admin, graph.organization_a),
    ):
        publish_configuration(
            clinic_id=graph.clinic_b,
            expected_version=0,
            content=ConfigurationContent(
                queue_quotas={"ai-batch": fairness.MAX_QUOTA_PER_MINUTE},
                **CONTENT,
            ),
        )
    assert clinic_b_only_admin.pk == graph.clinic_admin
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 60}


def test_org_wide_admin_can_set_quotas(rbac_graph: RbacGraph) -> None:
    """An admin holding a role in every clinic of the org may set quotas."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    published = _publish(
        graph,
        graph.clinic_b,
        actor=org_admin,
        queue_quotas={"ai-batch": 90},
    )
    assert published.queue_quotas == {"ai-batch": 90}
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 90}


def test_ordinary_settings_save_never_rewrites_org_quotas(
    rbac_graph: RbacGraph,
) -> None:
    """An unrelated clinic settings edit must not resurrect stale quotas."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_b,
        actor=org_admin,
        queue_quotas={"ai-batch": 100},
    )
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60},
    )
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 60}
    # An ordinary clinic-B settings save (no quota field) by an admin who
    # can only see clinic B must not restore the older 100 quota. The
    # carry-forward read must follow the organization-scoped resolver,
    # not the actor's RLS-filtered view of other clinics' snapshots.
    clinic_b_only_admin = User.objects.get(pk=graph.clinic_admin)
    _publish(
        graph,
        graph.clinic_b,
        actor=clinic_b_only_admin,
        expected_version=1,
    )
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 60}
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        latest_b = latest_configuration(graph.clinic_b)
    assert latest_b is not None
    assert latest_b.queue_quotas == {"ai-batch": 60}


def test_publish_configuration_stores_and_carries_quotas(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    first = _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60, "bulk": 120},
    )
    assert first.queue_quotas == {"ai-batch": 60, "bulk": 120}
    second = _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        expected_version=1,
    )
    assert second.queue_quotas == {"ai-batch": 60, "bulk": 120}
    third = _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        expected_version=2,
        queue_quotas={"ai-batch": 30},
    )
    assert third.queue_quotas == {"ai-batch": 30}


def test_forged_quota_rejected_by_service_and_database(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    with runtime_role(), tenant_context(org_admin.pk, graph.organization_a):
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
            {"ai-batch": [60]},
            {"ai-batch": "60"},
            {"ai-batch": True},
            {"ai-batch": None},
            {"ai-batch": {"nested": 1}},
            {"unknown-queue": 10},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                ClinicConfiguration.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    version=1,
                    display_name="Safe",
                    queue_quotas=forged,
                    published_by_id=org_admin.pk,
                )
        assert not ClinicConfiguration.objects.exists()


@pytest.mark.parametrize(
    "payload",
    [
        '{"ai-batch": 60.0}',
        '{"ai-batch": 1.5}',
        '{"ai-batch": 0}',
        '{"ai-batch": -1}',
        '{"ai-batch": 999999}',
    ],
)
def test_database_rejects_integral_float_and_out_of_range_quotas(
    rbac_graph: RbacGraph, payload: str
) -> None:
    """The CHECK must reject what the service and worker reject.

    A service-published map can only carry Python ints, so DB parity is
    proven with the literal JSON text: ``60.0`` is an integral float the
    service rejects and the worker would defer as malformed; ``1e2`` is
    covered separately because jsonb normalizes it to the int 100, which
    the worker accepts.
    """
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    with (
        runtime_role(),
        tenant_context(org_admin.pk, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        _insert_config_row(cursor=cursor, graph=graph, admin=org_admin, payload=payload)


def _insert_config_row(
    *, cursor: CursorWrapper, graph: RbacGraph, admin: User, payload: str
) -> None:
    """Insert one configuration row with literal JSON quota text."""
    cursor.execute(
        "INSERT INTO clinic_app.identity_clinicconfiguration "
        "(id, organization_id, clinic_id, version, display_name, "
        "contact_email, contact_phone, brand_token, reminder_hours, "
        "queue_quotas, logo_png, published_by_id, created_at) "
        "VALUES (gen_random_uuid(), %s, %s, 1, 'Safe', '', '', 'navy', 24, "
        "CAST(%s AS jsonb), %s, %s, now())",
        [
            str(graph.organization_a),
            str(graph.clinic_a),
            payload,
            "",
            str(admin.pk),
        ],
    )


def test_database_accepts_scientific_notation_integral_json(
    rbac_graph: RbacGraph,
) -> None:
    """jsonb normalizes ``1e2`` to 100; the stored value is a usable int."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    with (
        runtime_role(),
        tenant_context(org_admin.pk, graph.organization_a),
        connection.cursor() as cursor,
    ):
        _insert_config_row(
            cursor=cursor,
            graph=graph,
            admin=org_admin,
            payload='{"ai-batch": 1e2}',
        )
    with runtime_role():
        assert fairness._organization_quotas(graph.organization_a) == {"ai-batch": 100}


def test_resolver_returns_latest_org_quotas(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60},
    )
    _publish(
        graph,
        graph.clinic_b,
        actor=org_admin,
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


def test_malformed_quota_still_denies_clinical_during_redis_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fairness, "_broker_url", lambda: f"redis://127.0.0.1:{_free_port()}/0"
    )
    monkeypatch.setattr(fairness, "_organization_quotas", lambda _org: {"clinical": 0})
    with runtime_role():
        assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is False
    monkeypatch.setattr(fairness, "_organization_quotas", lambda _org: [60])
    with runtime_role():
        assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is False


def test_deferral_log_volume_is_bounded_by_closed_labels(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(fairness, "_defer_log_times", {})
    with caplog.at_level(logging.INFO, logger=fairness.__name__):
        for _ in range(1000):
            fairness._emit_metric(
                "clinic_fairness.deferred", queue="ai-batch", reason="quota_exceeded"
            )
        assert len(caplog.records) == 1
        clock["t"] += 1.0
        fairness._emit_metric(
            "clinic_fairness.deferred", queue="ai-batch", reason="quota_exceeded"
        )
        fairness._emit_metric(
            "clinic_fairness.deferred", queue="bulk", reason="quota_exceeded"
        )
        fairness._emit_metric(
            "clinic_fairness.deferred", queue="ai-batch", reason="malformed_quota"
        )
    assert len(caplog.records) == 4
    assert set(fairness._defer_log_times) == {
        ("ai-batch", "quota_exceeded"),
        ("bulk", "quota_exceeded"),
        ("ai-batch", "malformed_quota"),
    }
    for record in caplog.records:
        assert isinstance(record.args, tuple)
        assert record.args[0] == "clinic_fairness.deferred"
        assert record.args[1] in fairness.QUEUE_NAMES
        assert record.args[2] in {"quota_exceeded", "malformed_quota"}


@GATED
def test_empty_bucket_refusals_skip_database_and_emit_every_metric(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    emitted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(fairness, "_organization_quotas", lambda _org: {"ai-batch": 1})
    monkeypatch.setattr(
        fairness,
        "_emit_metric",
        lambda name, *, queue, reason: emitted.append((name, queue, reason)),
    )
    with runtime_role():
        assert fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization_id}:ai-batch"
    before = client.hgetall(key)
    client.pexpire(key, 5000)

    def forbidden(_organization_id: UUID) -> object:
        pytest.fail("an impossible admission reached the quota resolver")

    monkeypatch.setattr(fairness, "_organization_quotas", forbidden)
    with runtime_role():
        assert not any(
            fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
            for _ in range(512)
        )
    assert client.hgetall(key) == before
    ttl = client.pttl(key)
    assert isinstance(ttl, int)
    assert ttl > 5000
    assert emitted == [("clinic_fairness.deferred", "ai-batch", "quota_exceeded")] * 512


@GATED
def test_negative_preflight_does_not_rewrite_a_fresh_bucket_expiry(
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    now = frozen_clock["t"]
    args = (now, fairness.MAX_QUOTA_PER_MINUTE / 60.0, "expiry-proof")
    client.hset(key, mapping={"tokens": 0, "touched": now})
    # Start close to expiry: denial must retain state, not let it reset full.
    client.pexpire(key, 5000)
    assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args) == 0
    ttl = client.pttl(key)
    assert isinstance(ttl, int)
    assert 5000 < ttl <= 121000

    # Seed ample headroom so the write-count assertion has no sleep or race
    # against the one-second refresh window.
    client.pexpire(key, 180000)
    before = client.info("commandstats")["cmdstat_pexpire"]["calls"]
    for _ in range(64):
        assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args) == 0
    assert client.info("commandstats")["cmdstat_pexpire"]["calls"] == before
    assert client.hgetall(key) == {b"tokens": b"0", b"touched": str(int(now)).encode()}


@GATED
def test_negative_preflight_never_refuses_when_actual_quota_admits(
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    """Real Lua checks include every threshold and a backwards-clock case."""
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    touched = frozen_clock["t"]
    quotas = (1, 2, 59, 60, 600, 9999, 99999, fairness.MAX_QUOTA_PER_MINUTE)
    balances = (-1.0, 0.0, 0.001, 0.5, 0.999, 1.0, 2.0, 100000.0)
    elapsed_times = (-0.001, 0.0, 0.0000005, 0.0005, 0.0006, 0.001, 1.0, 60.0)
    for quota, tokens, elapsed in product(quotas, balances, elapsed_times):
        client.hset(key, mapping={"tokens": tokens, "touched": touched})
        now = touched + elapsed
        possible = bool(
            client.eval(
                fairness._CREDIT_POSSIBLE_LUA,
                1,
                key,
                now,
                fairness.MAX_QUOTA_PER_MINUTE / 60.0,
                "bound-proof",
            )
        )
        actual = bool(
            client.eval(
                fairness._BUCKET_LUA, 1, key, quota, now, quota / 60.0, "bound-proof"
            )
        )
        assert possible or not actual, (quota, tokens, elapsed)


@GATED
def test_exhausted_bucket_observes_published_quota_increase_immediately(
    rbac_graph: RbacGraph,
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    graph = rbac_graph
    admin = _org_admin(graph, graph.organization_a)
    _publish(graph, graph.clinic_a, actor=admin, queue_quotas={"ai-batch": 1})
    with runtime_role():
        assert fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
        assert not fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
    _publish(
        graph,
        graph.clinic_a,
        actor=admin,
        expected_version=1,
        queue_quotas={"ai-batch": fairness.MAX_QUOTA_PER_MINUTE},
    )
    frozen_clock["t"] += 0.001
    with runtime_role():
        assert fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )


@GATED
def test_credited_bucket_observes_published_quota_decrease_immediately(
    rbac_graph: RbacGraph,
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    graph = rbac_graph
    admin = _org_admin(graph, graph.organization_a)
    _publish(graph, graph.clinic_a, actor=admin, queue_quotas={"ai-batch": 600})
    with runtime_role():
        assert fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
    _publish(
        graph,
        graph.clinic_a,
        actor=admin,
        expected_version=1,
        queue_quotas={"ai-batch": 1},
    )
    with runtime_role():
        assert fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
        assert not fairness.fair_acquire(
            organization_id=graph.organization_a, queue="ai-batch"
        )
    key = f"{fairness._BUCKET_KEY_PREFIX}:{graph.organization_a}:ai-batch"
    remaining = fairness._client().hget(key, "tokens")
    assert isinstance(remaining, (bytes, str))
    assert float(remaining) == 0


@GATED
def test_possible_credit_uses_fresh_quota_on_every_admission(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    quota = {"value": 600}
    reads: list[UUID] = []

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": quota["value"]}

    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    with runtime_role():
        assert fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
        # Cached executable Lua must reload after a flush and must never
        # cache the authority value alongside the code.
        fairness._client().script_flush()
        quota["value"] = 1
        assert fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
        assert not fairness.fair_acquire(
            organization_id=organization_id, queue="ai-batch"
        )
    assert reads == [organization_id, organization_id]
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization_id}:ai-batch"
    tokens = fairness._client().hget(key, "tokens")
    assert isinstance(tokens, (bytes, str))
    assert float(tokens) == 0


def test_redis_loss_after_preflight_retains_quota_and_outage_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def eval_script(*args: object) -> int:
        calls.append(args[0])
        if len(calls) % 2:
            return 1
        message = "synthetic outage"
        raise redis.ConnectionError(message)

    monkeypatch.setattr(fairness, "_client", SimpleNamespace)
    monkeypatch.setattr(
        fairness,
        "_evaluate",
        lambda _client, source, key, *args: eval_script(source, 1, key, *args),
    )
    monkeypatch.setattr(fairness, "_organization_quotas", lambda _org: {})
    assert fairness.fair_acquire(organization_id=uuid4(), queue="ai-batch") is False
    assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is True
    assert len(calls) == 4
    monkeypatch.setattr(fairness, "_organization_quotas", lambda _org: [60])
    assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is False
    # Malformed authority never reaches a spend or its clinical fail-open.
    assert len(calls) == 6
    assert calls[-1] == fairness._RELEASE_RESOLVER_LUA


@GATED
@pytest.mark.parametrize("available", [False, True])
def test_contended_decision_rechecks_credit_without_reusing_authority(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    organization_id = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization_id}:ai-batch"
    quota = {"value": 3 if available else 1}
    monkeypatch.setattr(
        fairness, "_organization_quotas", lambda _org: {"ai-batch": quota["value"]}
    )
    with runtime_role():
        assert fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
    frozen_clock["t"] += 0.001
    entered, release, contended = (threading.Event() for _ in range(3))
    reads: list[int] = []
    client = fairness._client()
    original_eval = client.evalsha
    credit_sha = fairness._script_hashes[fairness._CREDIT_POSSIBLE_LUA]

    def observe(script: str, numkeys: int, *args: str | float | bytes) -> object:
        outcome = original_eval(script, numkeys, *args)
        if script == credit_sha and outcome == 2:
            contended.set()
        return outcome

    def resolve(_organization_id: UUID) -> object:
        value = quota["value"]
        reads.append(value)
        if len(reads) == 1:
            # Hold the advisory deadline explicitly; event scheduling must
            # not depend on the real Redis clock advancing by less than 1s.
            client.hset(key, "resolve_until", 3_000_000_000)
            entered.set()
            assert release.wait(timeout=10)
        return {"ai-batch": value}

    def acquire() -> bool:
        try:
            with runtime_role():
                return fairness.fair_acquire(
                    organization_id=organization_id, queue="ai-batch"
                )
        finally:
            connections.close_all()

    monkeypatch.setattr(client, "evalsha", observe)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(acquire)
        try:
            assert entered.wait(timeout=10)
            waiter = pool.submit(acquire)
            assert contended.wait(timeout=10)
            assert len(reads) == 1
            if available:
                # The waiting decision must read the newly lowered quota,
                # rather than reuse its predecessor's in-flight result.
                quota["value"] = 1
        finally:
            release.set()
        assert leader.result(timeout=10) is available
        assert waiter.result(timeout=10) is available
    assert reads == ([3, 1] if available else [1])
    if available:
        remaining = client.hget(key, "tokens")
        assert isinstance(remaining, (bytes, str))
        assert float(remaining) == 0


@GATED
def test_expired_decision_slot_requires_fresh_authority(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization_id}:ai-batch"
    client.hset(
        key,
        mapping={
            "tokens": 0,
            "touched": frozen_clock["t"] - 60,
            "resolving": "departed-worker",
            "resolve_until": 0,
        },
    )
    reads: list[UUID] = []

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": 1}

    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    with runtime_role():
        assert fairness.fair_acquire(organization_id=organization_id, queue="ai-batch")
    assert reads == [organization_id]
    assert client.hget(key, "resolving") is None
    client.hset(key, mapping={"resolving": "current-worker", "resolve_until": 1})
    client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "departed-worker")
    assert client.hget(key, "resolving") == b"current-worker"


@GATED
def test_token_bucket_enforces_per_org_quota(
    rbac_graph: RbacGraph,
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    """Two organizations share one broker but never share a bucket."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60},
    )
    org_admin_b = _admin(graph, graph.clinic_c, graph.organization_b)
    _publish(
        graph,
        graph.clinic_c,
        actor=org_admin_b,
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
    frozen_clock: dict[str, float],
) -> None:
    """Refill is driven by the injected clock, never by sleeping."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
        queue_quotas={"ai-batch": 60},
    )
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
        frozen_clock["t"] += 30.0  # half a minute -> 30 tokens at 60/minute
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
    rbac_graph: RbacGraph,
    metered_broker: str,
    frozen_clock: dict[str, float],
) -> None:
    """A Barrier race on one bucket admits exactly the quota."""
    graph = rbac_graph
    org_admin = _org_admin(graph, graph.organization_a)
    _publish(
        graph,
        graph.clinic_a,
        actor=org_admin,
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


@GATED
def test_malformed_stored_quota_fails_closed(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed stored value defers instead of raising the allowance.

    The CHECK constraint blocks malformed maps at write time; this covers
    the defense-in-depth read path for a value that could only appear via
    a future schema change or manual repair.
    """
    monkeypatch.setattr(
        fairness,
        "_organization_quotas",
        lambda organization_id: {"ai-batch": [60], "bulk": 1},
    )
    with runtime_role():
        # The malformed queue defers; a well-formed sibling still applies.
        assert fairness.fair_acquire(organization_id=uuid4(), queue="ai-batch") is False
        assert fairness.fair_acquire(organization_id=uuid4(), queue="bulk") is True
        # A malformed non-dict map defers every queue.
        monkeypatch.setattr(
            fairness, "_organization_quotas", lambda organization_id: [60]
        )
        assert fairness.fair_acquire(organization_id=uuid4(), queue="bulk") is False
        assert fairness.fair_acquire(organization_id=uuid4(), queue="clinical") is False
