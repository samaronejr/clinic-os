"""Aggregate per-app tenant posture declarations into one registry.

Every installed domain app that owns models must ship an ``rls.py`` module
declaring:

- ``RLS_TARGETS``: ``(table, tenant_column)`` pairs carrying the shared
  ``tenant_isolation`` policy;
- ``CUSTOM_RLS_TABLES``: tables carrying bespoke FORCE-RLS policies;
- ``NON_RLS_TABLES``: tables intentionally without RLS (resolver-only);
- ``RUNTIME_GRANTS``: ``table -> frozenset`` of the table-level privileges
  held by the ``clinic_app`` runtime role;
- ``COLUMN_GRANTS``: ``(table, column, privilege)`` triples of the
  column-level privileges held by the runtime role.

Posture tests call these functions; they read the populated Django app
registry, so they must not run at import time or inside migrations.
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Final

from django.apps import apps as django_apps
from django.apps.config import AppConfig

APP_SCHEMA: Final = "clinic_app"
RUNTIME_ROLE: Final = "clinic_app"
TENANT_ISOLATION_POLICY: Final = "tenant_isolation"
# Privileges the runtime role must never hold on domain tables. The single
# documented exception is the tenancy_tenantprobe sentinel, which keeps full
# DML so tests can prove cross-tenant writes are blocked by RLS.
FORBIDDEN_RUNTIME_PRIVILEGES: Final[frozenset[str]] = frozenset(
    {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
FORBIDDEN_GRANT_EXEMPTIONS: Final[frozenset[str]] = frozenset({"tenancy_tenantprobe"})


@dataclass(frozen=True, slots=True)
class AppPosture:
    """One domain app's declared tenant posture."""

    label: str
    rls_targets: frozenset[tuple[str, str]]
    custom_rls_tables: frozenset[str]
    non_rls_tables: frozenset[str]
    runtime_grants: dict[str, frozenset[str]]
    column_grants: frozenset[tuple[str, str, str]]


def _domain_app_configs() -> list[AppConfig]:
    """Return installed ``apps.*`` configs that own at least one model."""
    return [
        app_config
        for app_config in django_apps.get_app_configs()
        if app_config.name.startswith("apps.") and list(app_config.get_models())
    ]


def undeclared_apps() -> frozenset[str]:
    """Return domain app labels that own models but ship no ``rls.py``."""
    missing: set[str] = set()
    for app_config in _domain_app_configs():
        module_name = f"{app_config.name}.rls"
        try:
            import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                missing.add(app_config.label)
            else:
                raise
    return frozenset(missing)


def app_postures() -> dict[str, AppPosture]:
    """Return the declared posture of every domain app that ships rls.py."""
    postures: dict[str, AppPosture] = {}
    for app_config in _domain_app_configs():
        module_name = f"{app_config.name}.rls"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        postures[app_config.label] = AppPosture(
            label=app_config.label,
            rls_targets=frozenset(module.RLS_TARGETS),
            custom_rls_tables=frozenset(module.CUSTOM_RLS_TABLES),
            non_rls_tables=frozenset(module.NON_RLS_TABLES),
            runtime_grants={
                table: frozenset(privileges)
                for table, privileges in module.RUNTIME_GRANTS.items()
            },
            column_grants=frozenset(module.COLUMN_GRANTS),
        )
    return postures


def expected_tenant_columns() -> dict[str, str]:
    """Return ``table -> tenant column`` for shared-policy tenant tables."""
    return {
        table: tenant_column
        for posture in app_postures().values()
        for table, tenant_column in posture.rls_targets
    }


def custom_rls_tables() -> frozenset[str]:
    """Return every declared bespoke-policy table across domain apps."""
    return frozenset(
        table
        for posture in app_postures().values()
        for table in posture.custom_rls_tables
    )


def non_rls_tables() -> frozenset[str]:
    """Return every declared no-RLS table across domain apps."""
    return frozenset(
        table for posture in app_postures().values() for table in posture.non_rls_tables
    )


def runtime_grants() -> dict[str, frozenset[str]]:
    """Return the merged ``table -> privileges`` runtime grant map."""
    grants: dict[str, frozenset[str]] = {}
    for posture in app_postures().values():
        grants.update(posture.runtime_grants)
    return grants


def column_grants() -> frozenset[tuple[str, str, str]]:
    """Return the merged ``(table, column, privilege)`` grant set."""
    return frozenset(
        grant for posture in app_postures().values() for grant in posture.column_grants
    )


def select_only_runtime_tables() -> frozenset[str]:
    """Return shared-policy tables where the runtime role may only read."""
    tenant_tables = set(expected_tenant_columns())
    return frozenset(
        table
        for table, privileges in runtime_grants().items()
        if table in tenant_tables and privileges == {"SELECT"}
    )


def select_insert_runtime_tables() -> frozenset[str]:
    """Return shared-policy tables where the runtime role may read+insert."""
    tenant_tables = set(expected_tenant_columns())
    return frozenset(
        table
        for table, privileges in runtime_grants().items()
        if table in tenant_tables and privileges == {"SELECT", "INSERT"}
    )
