from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.subscriptions.models import Subscription
from django.db import connection, transaction

from identity.permission_support import permission_context
from patient_service_support import runtime_role
from subscriptions.catalog import discover_catalog

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_exact_catalog_is_the_discovered_force_rls_and_grant_snapshot() -> None:
    assert discover_catalog() == json.loads(
        Path(__file__).with_name("posture.json").read_text()
    )


def test_missing_foreign_and_inactive_tenant_context_exposes_no_commercial_rows(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), transaction.atomic():
        assert not Subscription.objects.exists()
    with permission_context(graph, graph.physician):
        assert list(Subscription.objects.values_list("organization_id", flat=True)) == [
            graph.organization_a
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [str(graph.organization_b)],
            )
        assert not Subscription.objects.exists()
