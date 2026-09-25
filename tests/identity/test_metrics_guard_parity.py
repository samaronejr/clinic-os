"""Explicit non-staff metrics authority and aggregate-only SQL oracles."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.comms.models import IntegrationOperation
from apps.core import telemetry
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.urls import resolve
from django.utils import timezone
from psycopg import sql

from auth.stepup_test_support import create_role_actor
from identity import legacy_guard_inventory
from identity.permission_support import owner_context
from identity.test_permission_parity import INVENTORY
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
METRICS_GUARDS = {
    "apps.core.telemetry._allowed_networks": "metrics_network",
    "apps.core.telemetry._client_ip_allowed": "metrics_network",
    "apps.core.telemetry._ops_token_valid": "metrics_token",
    "apps.core.telemetry.internal_metrics": "metrics_http",
}
METRICS_SQL_ORACLES = {
    "clinic_app.comms_operation_state_counts_v1": [
        "operation_state_counts",
        "operation_state_counts_acl",
    ],
}
COUNTS_QUERY = (
    "SELECT * FROM clinic_app.comms_operation_state_counts_v1() ORDER BY status"
)


@pytest.fixture
def ops_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = f"synthetic-ops-{uuid4().hex}"
    monkeypatch.setenv(telemetry.OPS_METRICS_TOKEN_ENV, token)
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "192.0.2.0/28")
    return token


def test_operations_classification_is_explicit_and_closed() -> None:
    classified = {
        row["symbol"]: row
        for row in INVENTORY["candidates"]
        if "operations_auth" in row["signals"]
    }
    assert set(classified) == set(METRICS_GUARDS)
    for symbol, oracle in METRICS_GUARDS.items():
        assert classified[symbol]["kind"] == "nonstaff"
        assert classified[symbol]["reason"]
        assert classified[symbol]["probes"] == [oracle]
    assert resolve("/internal/metrics").func is telemetry.internal_metrics


def test_future_operations_guard_is_not_silently_exempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "apps/example/views.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def future_guard(request):\n    return telemetry._ops_token_valid(request)\n"
    )
    monkeypatch.setattr(legacy_guard_inventory, "ROOT", tmp_path)
    discovered = legacy_guard_inventory.discover()
    assert discovered == {"apps.example.views.future_guard": ["operations_auth"]}
    assert not set(discovered) <= {row["symbol"] for row in INVENTORY["candidates"]}


def test_metrics_token_oracle(ops_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    request = RequestFactory().get(
        "/internal/metrics", HTTP_AUTHORIZATION=f"Bearer {ops_token}"
    )
    assert telemetry._ops_token_valid(request) is True
    request.META["HTTP_AUTHORIZATION"] = "Bearer invalid"
    assert telemetry._ops_token_valid(request) is False
    request.META["HTTP_AUTHORIZATION"] = f"Bearer {ops_token}"
    monkeypatch.delenv(telemetry.OPS_METRICS_TOKEN_ENV)
    assert telemetry._ops_token_valid(request) is False


def test_metrics_network_oracle(
    ops_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RequestFactory().get("/internal/metrics", REMOTE_ADDR="192.0.2.3")
    assert telemetry._client_ip_allowed(request) is True
    request.META.update(REMOTE_ADDR="192.0.2.80", HTTP_X_FORWARDED_FOR="192.0.2.3")
    assert telemetry._client_ip_allowed(request) is False
    request.META["REMOTE_ADDR"] = "192.0.2.3"
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "not-a-network")
    assert telemetry._client_ip_allowed(request) is False


@pytest.mark.parametrize("role", [*UserClinicRole.Role.values, None])
@override_settings(ALLOWED_HOSTS=["testserver"], CELERY_BROKER_URL="memory://")
def test_metrics_http_uses_ops_authority_not_staff_roles(
    rbac_graph: RbacGraph,
    role: str | None,
    ops_token: str,
) -> None:
    client = Client()
    if role is not None:
        actor = create_role_actor(rbac_graph, UserClinicRole.Role(role))
        with runtime_role():
            client.force_login(
                actor, backend="apps.identity.auth_backends.ClinicBackend"
            )
    with runtime_role():
        allowed = client.get(
            "/internal/metrics",
            REMOTE_ADDR="192.0.2.3",
            HTTP_AUTHORIZATION=f"Bearer {ops_token}",
        )
        assert allowed.status_code == 200
        assert (
            client.get("/internal/metrics", REMOTE_ADDR="192.0.2.3").status_code == 401
        )
        assert (
            client.get(
                "/internal/metrics",
                REMOTE_ADDR="192.0.2.80",
                HTTP_AUTHORIZATION=f"Bearer {ops_token}",
            ).status_code
            == 404
        )


def _operation_counts() -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(COUNTS_QUERY)
        assert cursor.description is not None
        assert [column.name for column in cursor.description] == [
            "status",
            "operation_count",
            "oldest_created_at",
        ]
        rows: list[tuple[object, ...]] = cursor.fetchall()
    return rows


def test_operation_state_counts_role_oracle(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    actors = [create_role_actor(graph, role) for role in UserClinicRole.Role]
    with runtime_role():
        assert _operation_counts() == []
    for organization, clinic, actor_id, status, year in [
        (graph.organization_a, graph.clinic_a, graph.physician, "pending", 2000),
        (graph.organization_a, graph.clinic_a, graph.physician, "pending", 2001),
        (graph.organization_b, graph.clinic_c, graph.shared_user, "pending", 1999),
        (graph.organization_b, graph.clinic_c, graph.shared_user, "failed", 2002),
    ]:
        stamp = datetime(year, 1, 1, tzinfo=UTC)
        with monkeypatch.context() as clock, owner_context(organization):
            clock.setattr(timezone, "now", lambda value=stamp: value)
            IntegrationOperation.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                actor_id=actor_id,
                channel="sms",
                provider="synthetic",
                subject_type="synthetic.metrics",
                subject_id=uuid4(),
                status=status,
                idempotency_key=uuid4(),
            )
    expected = [
        ("failed", 1, datetime(2002, 1, 1, tzinfo=UTC)),
        ("pending", 3, datetime(1999, 1, 1, tzinfo=UTC)),
    ]
    with runtime_role():
        assert IntegrationOperation.objects.count() == 0  # No tenant grants leaked.
        assert _operation_counts() == expected
    for actor in actors:
        with runtime_role(), tenant_context(actor.pk, graph.organization_a):
            assert IntegrationOperation.objects.count() == 2
            assert _operation_counts() == expected


def test_operation_state_counts_requires_execute_grant(
    superuser_database_url: str,
) -> None:
    # A schema-USAGE-only principal must not obtain the sessionless aggregate.
    # Role and grant DDL live in one rolled-back, task-database transaction.
    role = sql.Identifier(f"synthetic_metrics_{uuid4().hex}")
    with psycopg.connect(superuser_database_url) as conn:
        try:
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA clinic_app TO {}").format(role))
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                conn.transaction(),
            ):
                conn.execute(COUNTS_QUERY).fetchall()
        finally:
            conn.rollback()
