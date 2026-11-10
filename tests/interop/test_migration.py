"""The interop schema reverses only without history and restores what it changed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor

from interop.support import local_bucket, make_client, org_admin

if TYPE_CHECKING:
    from interop.support import LocalBucket
    from rbac_fixtures import RbacGraph

__all__ = ("local_bucket",)
pytestmark = pytest.mark.django_db(transaction=True)

CATALOG_QUERIES = {
    "table_grants": """
        SELECT grantee, table_name, privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema = 'clinic_app' ORDER BY 1, 2, 3
    """,
    "column_grants": """
        SELECT grantee.rolname, class.relname, attribute.attname, acl.privilege_type
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS class ON class.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
        JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
        WHERE namespace.nspname = 'clinic_app' ORDER BY 1, 2, 3, 4
    """,
    "policies": """
        SELECT tablename, policyname, cmd, roles::text, qual, with_check
        FROM pg_catalog.pg_policies
        WHERE schemaname = 'clinic_app' ORDER BY 1, 2
    """,
    "functions": """
        SELECT procedure.proname,
               pg_catalog.pg_get_function_identity_arguments(procedure.oid),
               procedure.proacl::text,
               pg_catalog.pg_get_functiondef(procedure.oid)
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'clinic_app' ORDER BY 1, 2
    """,
    "triggers": """
        SELECT class.relname, item.tgname, pg_catalog.pg_get_triggerdef(item.oid)
        FROM pg_catalog.pg_trigger AS item
        JOIN pg_catalog.pg_class AS class ON class.oid = item.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        WHERE namespace.nspname = 'clinic_app' AND NOT item.tgisinternal
        ORDER BY 1, 2
    """,
}


def _catalog() -> dict[str, list[tuple[object, ...]]]:
    with connection.cursor() as cursor:
        snapshot = {}
        for name, query in CATALOG_QUERIES.items():
            cursor.execute(query)
            snapshot[name] = list(cursor.fetchall())
    return snapshot


def test_unused_interop_schema_reverses_and_reapplies_to_the_exact_catalog() -> None:
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    before = _catalog()
    try:
        executor.migrate([("interop", None)])
        reversed_catalog = _catalog()
        assert not [
            row
            for rows in reversed_catalog.values()
            for row in rows
            if "interop" in repr(row)
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_column_privilege('clinic_resolver',"
                " 'clinic_app.consent_aiusedisclosure','refused','SELECT'),"
                " has_table_privilege('clinic_resolver',"
                " 'clinic_app.consent_aiusedisclosure','SELECT'),"
                " has_column_privilege('clinic_resolver',"
                " 'clinic_app.intake_demographicscorrection','demographics_id',"
                " 'SELECT'),"
                " has_table_privilege('clinic_resolver',"
                " 'clinic_app.comms_internalnote','SELECT')"
            )
            assert cursor.fetchone() == (True, False, True, False)
    finally:
        MigrationExecutor(connection).migrate(targets)
    assert _catalog() == before


def test_retained_partner_history_refuses_schema_removal(
    rbac_graph: RbacGraph, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    make_client(graph, org_admin(graph))
    executor = MigrationExecutor(connection)
    target = ("interop", "0001_external_api_export")
    migration = executor.loader.get_migration(*target)
    before = _catalog()
    with (
        pytest.raises(DatabaseError, match="interop history requires restore"),
        connection.schema_editor() as editor,
    ):
        migration.unapply(executor.loader.project_state([target]), editor)
    assert _catalog() == before
