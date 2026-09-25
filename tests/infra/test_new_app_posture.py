"""Posture framework guards for current and future domain apps.

Every installed domain app that owns models must declare its tenant posture
in ``apps/<app>/rls.py`` (``RLS_TARGETS``, ``CUSTOM_RLS_TABLES``,
``NON_RLS_TABLES``, ``RUNTIME_GRANTS``). These tests prove the declarations
are complete, that bespoke-policy tables really are FORCE-RLS in the
database, and that the runtime role's table grants match the declarations
exactly - so a new app or table cannot silently weaken RLS or grants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.tenancy import migrations_support, posture
from apps.tenancy.models import TenantProbe, TenantScopedModel
from django.apps import apps as django_apps
from django.db import connection, transaction

from tenant_probe_support import assert_no_cross_tenant_rows

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _domain_model_tables() -> dict[str, set[str]]:
    """Return ``app label -> model table names`` for domain apps."""
    tables: dict[str, set[str]] = {}
    for app_config in django_apps.get_app_configs():
        if not app_config.name.startswith("apps."):
            continue
        models = list(app_config.get_models())
        if models:
            tables[app_config.label] = {model._meta.db_table for model in models}
    return tables


def _tenant_scoped_tables() -> set[str]:
    """Return db_table of every concrete TenantScopedModel."""
    return {
        model._meta.db_table
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    }


def test_every_domain_app_with_models_declares_posture() -> None:
    # Given: the installed domain apps that own models
    # When: the posture registry looks for each app's rls.py
    # Then: none is missing its declaration module
    assert posture.undeclared_apps() == frozenset()


def test_every_model_table_has_a_declared_rls_posture() -> None:
    # Given: every model table of every domain app
    model_tables = set().union(*_domain_model_tables().values())
    postures = posture.app_postures()

    # When: declared RLS coverage is unioned across apps
    rls_tables = {
        table for posture_ in postures.values() for table, _ in posture_.rls_targets
    }
    custom_tables = posture.custom_rls_tables()
    non_rls_tables = posture.non_rls_tables()

    # Then: the three declared sets partition the model tables exactly
    assert not rls_tables & custom_tables
    assert not rls_tables & non_rls_tables
    assert not custom_tables & non_rls_tables
    assert model_tables == rls_tables | custom_tables | non_rls_tables
    # And: every tenant-scoped table is declared under some RLS regime
    assert _tenant_scoped_tables() <= rls_tables | custom_tables


def test_custom_rls_tables_are_forced_in_the_database() -> None:
    # Given: every table declared under a bespoke RLS policy
    declared = posture.custom_rls_tables()

    # When: the catalog flags are read
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT class.relname
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s
              AND class.relkind = 'r'
              AND class.relrowsecurity
              AND class.relforcerowsecurity
            """,
            [posture.APP_SCHEMA],
        )
        forced = {row[0] for row in cursor.fetchall()}

    # Then: each declared table is really ENABLE+FORCE RLS
    assert declared <= forced


def test_runtime_grants_match_declarations_exactly() -> None:
    # Given: every domain model table and the declared grant map
    model_tables = set().union(*_domain_model_tables().values())
    declared = posture.runtime_grants()

    # When: the runtime role's actual table grants are enumerated
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = %s
              AND table_schema = %s
              AND table_name = ANY(%s)
            """,
            [posture.RUNTIME_ROLE, posture.APP_SCHEMA, sorted(model_tables)],
        )
        actual: dict[str, set[str]] = {}
        for table, privilege in cursor.fetchall():
            actual.setdefault(table, set()).add(privilege)

    # Then: every model table is declared and grants match exactly
    assert model_tables == set(declared)
    for table in sorted(model_tables):
        assert actual.get(table, set()) == set(declared[table]), (
            f"{table}: runtime grants {sorted(actual.get(table, set()))} "
            f"!= declared {sorted(declared[table])}"
        )


def test_no_forbidden_runtime_privileges_are_declared() -> None:
    # Given: the merged runtime grant declarations
    # When: forbidden privileges are intersected per table
    # Then: only the documented probe sentinel keeps DELETE
    offenders = {
        (table, privilege)
        for table, privileges in posture.runtime_grants().items()
        for privilege in privileges & posture.FORBIDDEN_RUNTIME_PRIVILEGES
        if table not in posture.FORBIDDEN_GRANT_EXEMPTIONS
    }
    assert offenders == set()


def test_revoke_default_dml_trims_bootstrap_defaults() -> None:
    # Given: the bootstrap default DML surface
    # When: the helper emits REVOKE SQL for a select-only table
    sql = migrations_support.revoke_default_dml("example_table", {"SELECT"})

    # Then: every non-kept default privilege is revoked from clinic_app
    assert sql == (
        "REVOKE INSERT, UPDATE, DELETE ON TABLE "
        "clinic_app.example_table FROM clinic_app;\n"
    )
    assert migrations_support.revoke_default_dml(
        "example_table", {"SELECT", "INSERT"}
    ) == ("REVOKE UPDATE, DELETE ON TABLE clinic_app.example_table FROM clinic_app;\n")
    assert (
        migrations_support.revoke_default_dml(
            "example_table", set(migrations_support.DEFAULT_DML_PRIVILEGES)
        )
        == "-- example_table: runtime role keeps the default DML grants\n"
    )
    with pytest.raises(ValueError, match="outside the bootstrap defaults"):
        migrations_support.revoke_default_dml("example_table", {"TRIGGER"})


def test_tenant_probe_cross_tenant_isolation(
    tenant_probe_pair: RbacGraph,
) -> None:
    # Given: two organizations each owning a TenantProbe row
    for org_id, label in (
        (tenant_probe_pair.organization_a, "a"),
        (tenant_probe_pair.organization_b, "b"),
    ):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(org_id)],
            )
            TenantProbe.objects.create(
                organization_id=org_id,
                label=f"synthetic-probe-{label}",
            )

    # When: the runtime role queries under each tenant context
    # Then: no row crosses the organization boundary
    assert_no_cross_tenant_rows(tenant_probe_pair, TenantProbe)
