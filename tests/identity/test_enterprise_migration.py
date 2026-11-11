"""Identity 0051 fences every clinic_app policy, keeps implicit checks, reverses unused.

The fence runs once, after every app's leaf, over every policy that applies to
clinic_app then; a policy that skips it would serve SCIM, pre-auth SSO or support
contexts through a generic tenant predicate. Policies for other roles only (owner
setup, the machine principal's reviewed gates) are never touched. Populated
authority history rolls back only by restore; an unused install reverses to the
prior catalog exactly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from apps.audit.migrations._v2_append_sql import INSTALL_V2_APPEND_SQL
from apps.identity.enterprise_contract import TABLES
from apps.identity.models import SsoConnection
from apps.workflows.migrations._coordination_sql import TASK_GUARD
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from identity.enterprise_support import enterprise_setup
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

LANE = ("identity", "0051_enterprise_governance")
BEFORE = ("identity", "0050_totp_secret_boundary")
FENCE = " AND enterprise_staff_context())"
# clinic_app ALL policies without WITH CHECK before 0051: PostgreSQL checked new
# rows with USING, so the fenced check must be exactly the fenced USING, never
# `true`. The owner-only physician_provision policy keeps its implicit check.
IMPLICIT_CHECKS = frozenset(
    {
        "analytics_analystreview.analytics_scope",
        "analytics_metricdefinitionversion.analytics_scope",
        "analytics_readmodelrefresh.analytics_scope",
        "analytics_savedreport.analytics_scope",
        "consent_aiusedisclosure.delegate_confidentiality",
        "consent_consenttext.delegate_purpose",
        "identity_physicianevidence.physician_evidence_assigned",
        "identity_physicianprofile.physician_self",
    }
)
OWNER_IMPLICIT_CHECK = "identity_physicianprofile.physician_provision"
# Task columns 0051 grants the resolver; INSERT and the state columns stay with
# the retention and data-request bridges that granted them first.
ADDED_TASK_COLUMNS = frozenset(
    f"{column}:UPDATE:clinic_resolver"
    for column in (
        "last_command_digest",
        "last_command_key",
        "owner_role",
        "owner_user_id",
    )
)
AUDIT_APPENDS = (
    "clinic_app.audit_append_unchecked_v2"
    "(text,text,inet,text,text,timestamptz,jsonb,bytea)",
    "clinic_app.audit_append_system_unchecked_v2"
    "(text,text,inet,text,text,timestamptz,jsonb,bytea)",
)

# (command, permissive, applies to clinic_app, USING, WITH CHECK)
Policy = tuple[str, bool, bool, str | None, str | None]


@dataclass(frozen=True, slots=True)
class Catalog:
    policies: dict[str, Policy]
    functions: dict[str, tuple[str, str, str | None]]
    triggers: dict[str, str]
    task_columns: list[str]


def _policies() -> dict[str, Policy]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname||'.'||p.polname,p.polcmd,p.polpermissive,"
            "(0::oid=ANY(p.polroles) OR EXISTS (SELECT 1 FROM unnest(p.polroles) r(oid)"
            " WHERE pg_has_role('clinic_app',r.oid,'USAGE'))),"
            "pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid) "
            "FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND c.relrowsecurity "
            "AND NOT c.relname=ANY(%s) ORDER BY 1",
            [list(TABLES)],
        )
        return {
            str(name): (str(command), bool(permissive), bool(applies), using, check)
            for name, command, permissive, applies, using, check in cursor.fetchall()
        }


def _catalog() -> Catalog:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid::regprocedure::text,pg_get_functiondef(p.oid),"
            "pg_get_userbyid(p.proowner),p.proacl::text FROM pg_proc p "
            "WHERE p.pronamespace='clinic_app'::regnamespace AND p.prokind='f' "
            "ORDER BY 1"
        )
        functions = {
            str(name): (hashlib.sha256(str(body).encode()).hexdigest(), owner, acl)
            for name, body, owner, acl in cursor.fetchall()
        }
        cursor.execute(
            "SELECT c.relname||'.'||t.tgname,pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND NOT t.tgisinternal "
            "ORDER BY 1"
        )
        triggers = {str(name): str(body) for name, body in cursor.fetchall()}
        cursor.execute(
            "SELECT a.attname||':'||x.privilege_type||':'||pg_get_userbyid(x.grantee) "
            "FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x "
            "WHERE a.attrelid='clinic_app.workflows_task'::regclass ORDER BY 1"
        )
        task_columns = [str(row[0]) for row in cursor.fetchall()]
    return Catalog(_policies(), functions, triggers, task_columns)


def _source(signature: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure", [signature]
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def test_fence_narrows_every_clinic_app_policy_and_keeps_implicit_checks() -> None:
    policies = _policies()
    assert set(policies) >= IMPLICIT_CHECKS | {OWNER_IMPLICIT_CHECK}
    for name, (command, _permissive, applies, using, check) in policies.items():
        if not applies:
            assert "enterprise_staff_context" not in f"{using} {check}", name
            continue
        if command in {"*", "r", "w", "d"}:
            assert using is not None, name
            assert using.startswith("("), name
            assert using.endswith(FENCE), name
        if command in {"*", "a", "w"}:
            assert check is not None, name
            assert check.endswith(FENCE), name
    for name in IMPLICIT_CHECKS:
        _command, _permissive, applies, using, check = policies[name]
        assert applies, name
        assert check == using, name
    assert policies[OWNER_IMPLICIT_CHECK][2:] == (
        False,
        policies[OWNER_IMPLICIT_CHECK][3],
        None,
    )


def test_unused_install_reverses_exactly_and_reapplies_identically() -> None:
    installed = _catalog()
    assert set(installed.task_columns) >= ADDED_TASK_COLUMNS
    try:
        MigrationExecutor(connection).migrate([BEFORE])
        restored = _catalog()
        assert not any(
            "enterprise_staff_context" in f"{using} {check}"
            for _command, _permissive, _applies, using, check in (
                restored.policies.values()
            )
        )
        assert not [name for name in restored.functions if "enterprise_" in name]
        assert not [name for name in restored.triggers if ".enterprise_" in name]
        # The prior coordination guard and audit append return verbatim.
        assert _source("clinic_app.workflows_guard()") == TASK_GUARD.split("$f$")[1]
        for signature in AUDIT_APPENDS:
            assert _source(signature) in INSTALL_V2_APPEND_SQL
        assert restored.task_columns == [
            column
            for column in installed.task_columns
            if column not in ADDED_TASK_COLUMNS
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM unnest(%s::text[]) t "
                "WHERE to_regclass('clinic_app.'||t) IS NOT NULL",
                [list(TABLES)],
            )
            assert cursor.fetchone() == (0,)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    assert _catalog() == installed


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_populated_install_refuses_rollback_before_any_change(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    installed = _catalog()
    with pytest.raises(DatabaseError, match="rolls back only by restore"):
        MigrationExecutor(connection).migrate([BEFORE])
    assert LANE in MigrationExecutor(connection).loader.applied_migrations
    assert _catalog() == installed
    with owner_context(rbac_graph.organization_a):
        assert SsoConnection.objects.filter(pk=setup.connection.pk).exists()
