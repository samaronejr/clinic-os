"""The session's seeded probe world is only ever handed out as it was seeded.

The template is locked after seeding (no connection is possible). A test
gets a fresh clone and a deep copy of the Python world. A template changed
anyway is refused, not served: each kind of change the certifier's verdicts
depend on is caught by its own digest class (a row, a function body, row
security, a grant, a policy, a trigger, a column default, a default ACL),
and with that class removed from the digest the same change is served, so a
missing class cannot go unnoticed. Closing a test's world drops its clones
and restores its connection.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import connection
from django.test import override_settings
from psycopg import sql

from database_urls import database_url_for_name
from identity import probe_worlds

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

type Admin = psycopg.Connection[tuple[object, ...]]
# A change to the template; it returns the call that undoes it.
type Plant = Callable[[Admin], Callable[[Admin], None]]


def _admin(database: str) -> Admin:
    return psycopg.connect(
        database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database),
        autocommit=True,
    )


@contextmanager
def _unlocked(name: str) -> Iterator[Admin]:
    """A superuser session on the locked template, locked again after."""
    allow = sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS {}")
    with _admin("postgres") as admin:
        admin.execute(allow.format(sql.Identifier(name), sql.SQL("true")))
    try:
        with _admin(name) as admin:
            yield admin
    finally:
        with _admin("postgres") as admin:
            admin.execute(allow.format(sql.Identifier(name), sql.SQL("false")))


def _one(admin: Admin, query: str) -> tuple[object, ...]:
    row = admin.execute(query).fetchone()
    assert row is not None, query
    return row


def _statements(do: str, undo: str) -> Plant:
    """A plant made of one statement and the statement that undoes it."""

    def plant(admin: Admin) -> Callable[[Admin], None]:
        admin.execute(do.encode())

        def restore(admin: Admin) -> None:
            admin.execute(undo.encode())

        return restore

    return plant


def _row(admin: Admin) -> Callable[[Admin], None]:
    key, name = _one(
        admin, "SELECT id, first_name FROM clinic_app.identity_user ORDER BY id LIMIT 1"
    )
    admin.execute(
        "UPDATE clinic_app.identity_user SET first_name = first_name || 'z' "
        "WHERE id = %s",
        [key],
    )

    def restore(admin: Admin) -> None:
        admin.execute(
            "UPDATE clinic_app.identity_user SET first_name = %s WHERE id = %s",
            [name, key],
        )

    return restore


def _function(admin: Admin) -> Callable[[Admin], None]:
    """The review's plant: clinic_app.audit_append's body replaced."""
    (definition,) = _one(
        admin,
        "SELECT pg_catalog.pg_get_functiondef(p.oid) FROM pg_catalog.pg_proc p "
        "WHERE p.pronamespace = 'clinic_app'::pg_catalog.regnamespace "
        "AND p.proname = 'audit_append'",
    )
    original = str(definition)
    changed = original.replace("BEGIN", "BEGIN\n    PERFORM 1;", 1)
    assert changed != original
    return _statements(changed, original)(admin)


def _policy(admin: Admin) -> Callable[[Admin], None]:
    (qual,) = _one(
        admin,
        "SELECT pg_catalog.pg_get_expr(polqual, polrelid) FROM pg_catalog.pg_policy "
        "WHERE polrelid = 'clinic_app.intake_clinicintakepolicy'::pg_catalog.regclass "
        "AND polname = 'tenant_isolation'",
    )
    alter = (
        "ALTER POLICY tenant_isolation ON clinic_app.intake_clinicintakepolicy USING "
    )
    return _statements(f"{alter}(({qual}) OR false)", f"{alter}({qual})")(admin)


# The review's plants (row security off, a grant revoked), and one per other
# catalog class the verdicts depend on.
_RLS = _statements(
    "ALTER TABLE clinic_app.intake_clinicintakepolicy DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE clinic_app.intake_clinicintakepolicy ENABLE ROW LEVEL SECURITY",
)
_GRANT = _statements(
    "REVOKE SELECT ON clinic_app.intake_clinicintakepolicy FROM clinic_app",
    "GRANT SELECT ON clinic_app.intake_clinicintakepolicy TO clinic_app",
)
_TRIGGER = _statements(
    "CREATE TRIGGER zz_stale_world BEFORE UPDATE "
    "ON clinic_app.intake_clinicintakepolicy FOR EACH ROW "
    "EXECUTE FUNCTION clinic_app.intake_identity_append_only_v1()",
    "DROP TRIGGER zz_stale_world ON clinic_app.intake_clinicintakepolicy",
)
_DEFAULT = _statements(
    "ALTER TABLE clinic_app.intake_clinicintakepolicy "
    "ALTER COLUMN required_fields SET DEFAULT '[]'::jsonb",
    "ALTER TABLE clinic_app.intake_clinicintakepolicy "
    "ALTER COLUMN required_fields DROP DEFAULT",
)
_DEFAULT_ACL = _statements(
    "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
    "GRANT SELECT ON TABLES TO clinic_resolver",
    "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
    "REVOKE SELECT ON TABLES FROM clinic_resolver",
)
_TABLE = _statements(
    "CREATE TABLE clinic_app.zz_stale_world (id integer)",
    "DROP TABLE clinic_app.zz_stale_world",
)


@pytest.mark.parametrize(
    ("plant", "digest_class"),
    [
        (_row, "rows"),
        (_function, "functions"),
        (_RLS, "relations"),
        (_GRANT, "relations"),
        (_policy, "policies"),
        (_TRIGGER, "triggers"),
        (_DEFAULT, "defaults"),
        (_DEFAULT_ACL, "default_acl"),
    ],
    ids=[
        "row",
        "function-body",
        "row-security",
        "grant",
        "policy",
        "trigger",
        "column-default",
        "default-acl",
    ],
)
def test_a_changed_template_is_refused_by_its_class(
    seeded_world: probe_worlds.SeededWorld, plant: Plant, digest_class: str
) -> None:
    with override_settings(**probe_worlds.SYNTHETIC):
        held = seeded_world.template()
        with _unlocked(held.name) as admin:
            undo = plant(admin)
        try:
            with pytest.raises(
                probe_worlds.StaleWorldError,
                match=rf"stale template {held.name}: its content changed after "
                rf"seeding \({digest_class}\)$",
            ):
                seeded_world()
            # Mutation: without its class the same clone is accepted (the
            # decision seeded_world() makes), so a class missing from
            # DIGEST_CLASSES would go unnoticed here.
            refused = str(connection.settings_dict["NAME"])
            with pytest.MonkeyPatch.context() as patch:
                patch.delitem(probe_worlds.DIGEST_CLASSES, digest_class)
                probe_worlds.check_clone(refused, held)
        finally:
            with _unlocked(held.name) as admin:
                undo(admin)
        seeded_world()  # the restored template is served again


def test_a_new_table_and_a_database_acl_are_refused(
    seeded_world: probe_worlds.SeededWorld,
) -> None:
    with override_settings(**probe_worlds.SYNTHETIC):
        seeded_world()
        held = probe_worlds.template_now()
        with _unlocked(held.name) as admin:
            undo = _TABLE(admin)
        try:
            with pytest.raises(probe_worlds.StaleWorldError, match="content changed"):
                seeded_world()
        finally:
            with _unlocked(held.name) as admin:
                undo(admin)
        grant = sql.SQL("{} TEMPORARY ON DATABASE {} {} clinic_app")
        with _admin("postgres") as admin:
            admin.execute(
                grant.format(sql.SQL("GRANT"), sql.Identifier(held.name), sql.SQL("TO"))
            )
        try:
            with pytest.raises(
                probe_worlds.StaleWorldError, match="environment changed"
            ):
                seeded_world()
        finally:
            with _admin("postgres") as admin:
                admin.execute(
                    grant.format(
                        sql.SQL("REVOKE"), sql.Identifier(held.name), sql.SQL("FROM")
                    )
                )
        seeded_world()


def test_the_template_is_locked(seeded_world: probe_worlds.SeededWorld) -> None:
    """No role can connect to the seeded template, so nothing in the suite
    can change it."""
    with override_settings(**probe_worlds.SYNTHETIC):
        seeded_world()
    held = probe_worlds.template_now()
    for variable in (
        "TEST_SUPERUSER_DATABASE_URL",
        "APP_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
    ):
        url = database_url_for_name(os.environ[variable], held.name)
        with pytest.raises(
            psycopg.OperationalError, match="not currently accepting connections"
        ):
            psycopg.connect(url).close()
    with _admin("postgres") as admin:
        row = admin.execute(
            "SELECT pg_catalog.count(*) FROM pg_catalog.pg_database d, "
            "pg_catalog.aclexplode(d.datacl) a WHERE d.datname = %s "
            "AND a.privilege_type = 'CONNECT'",
            [held.name],
        ).fetchone()
    assert row == (0,)
    assert held.grantees


def test_each_test_gets_its_own_clone_and_leaves_none(
    seeded_world: probe_worlds.SeededWorld,
) -> None:
    with override_settings(**probe_worlds.SYNTHETIC):
        seeded_world()
        first = str(connection.settings_dict["NAME"])
        world = seeded_world()
        second = str(connection.settings_dict["NAME"])
    held = probe_worlds.template_now()
    assert first != second
    assert {first, second} == set(probe_worlds.leftovers())
    assert all(name.startswith(probe_worlds.CLONE_PREFIX) for name in (first, second))
    assert world.matrix.states == held.world.matrix.states
    assert world is not held.world
    assert world.matrix.applied is not held.world.matrix.applied


def test_closing_a_world_drops_its_clones_and_restores_the_connection(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    probe_world_secrets: Path,
) -> None:
    source = str(connection.settings_dict["NAME"])
    factory = probe_worlds.WorldFactory(rbac_graph, monkeypatch, probe_world_secrets)
    try:
        with override_settings(**probe_worlds.SYNTHETIC):
            factory()
            factory()
        assert len(probe_worlds.leftovers()) == 2
    finally:
        factory.close()
    factory.close()  # idempotent
    assert probe_worlds.leftovers() == []
    assert str(connection.settings_dict["NAME"]) == source
