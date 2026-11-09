"""Protected rollback refuses, and the additive authority reverse retains Wave D."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.migrations._inventory_permission_sql import REVERSE_SQL, SQL
from django.db import connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from inventory.support import stock_world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_protected_inventory_reverse_refuses_without_removing_schema() -> None:
    executor = MigrationExecutor(connection)
    assert ("inventory", "0001_inventory") in executor.loader.applied_migrations
    try:
        with pytest.raises(IrreversibleError):
            executor.migrate([("inventory", None)])
    finally:
        # Reversible descendants (identity 0051, which follows every app's
        # leaf) precede the protected migration in the rollback plan.
        MigrationExecutor(connection).migrate(executor.loader.graph.leaf_nodes())
    assert ("inventory", "0001_inventory") in MigrationRecorder(
        connection
    ).applied_migrations()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_class WHERE "
            "relnamespace='clinic_app'::regnamespace "
            "AND relkind='r' AND relname LIKE %s",
            ["inventory_%"],
        )
        assert cursor.fetchone() == (12,)


def test_permission_reverse_and_forward_retain_current_wrapper_and_privacy(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.has_permission(text,uuid,uuid)'::regprocedure)"
        )
        wrapper = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(rbac_graph.organization_a), str(world.manager)],
        )
        cursor.execute(REVERSE_SQL)
        cursor.execute(
            "SELECT clinic_app.has_permission('privacy.requests',%s,NULL),"
            "clinic_app.has_permission('inventory.manage',%s,NULL)",
            [rbac_graph.clinic_a, rbac_graph.clinic_a],
        )
        assert cursor.fetchone() == (True, False)
        cursor.execute(SQL)
        cursor.execute(
            "SELECT clinic_app.has_permission('privacy.requests',%s,NULL),"
            "clinic_app.has_permission('inventory.manage',%s,NULL)",
            [rbac_graph.clinic_a, rbac_graph.clinic_a],
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.has_permission(text,uuid,uuid)'::regprocedure)"
        )
        assert cursor.fetchone() == wrapper
        transaction.set_rollback(True)
