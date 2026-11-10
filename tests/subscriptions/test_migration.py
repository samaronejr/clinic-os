from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole
from apps.subscriptions.models import Seat, Subscription
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from identity.permission_support import owner_context
from subscriptions.catalog import discover_catalog

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_populated_additive_reverse_forward_preserves_identity_and_installs_posture(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    before = discover_catalog()
    memberships: set[UUID] = set()
    for organization in (graph.organization_a, graph.organization_b):
        with owner_context(organization):
            memberships.update(UserClinicRole.objects.values_list("pk", flat=True))
    try:
        MigrationExecutor(connection).migrate([("subscriptions", None)])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='clinic_app' AND c.relkind='r' "
                "AND c.relname LIKE 'subscriptions_%'"
            )
            assert cursor.fetchone() == (0,)
    finally:
        # The reverse also unapplies what follows subscriptions: interop 0002 and
        # 0003, and identity 0051 (it follows every app's leaf to fence every
        # policy), so the whole graph comes back, not one app.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    assert discover_catalog() == before
    preserved: set[UUID] = set()
    for organization in (graph.organization_a, graph.organization_b):
        with owner_context(organization):
            preserved.update(UserClinicRole.objects.values_list("pk", flat=True))
    assert preserved == memberships
    assert set(Subscription.objects.values_list("organization_id", "state")) == {
        (graph.organization_a, "trial"),
        (graph.organization_b, "trial"),
    }
    assert set(Seat.objects.values_list("membership_id", flat=True)) == memberships
