"""The session's seeded probe world is only ever handed out as it was seeded.

A test gets a fresh clone of the template and a deep copy of the Python
world; a template altered after seeding (a row, a table, its database ACL)
is refused, not served; and closing a test's world drops its clones and
restores its connection.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import connection
from django.test import override_settings
from psycopg import sql

from database_urls import database_url_for_name
from identity import probe_worlds

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _admin(database: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database),
        autocommit=True,
    )


def _row(name: str) -> Callable[[], None]:
    """Change one user's name in the template; the returned call restores it."""
    with _admin(name) as admin:
        row = admin.execute(
            "SELECT id, first_name FROM clinic_app.identity_user ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        admin.execute(
            "UPDATE clinic_app.identity_user SET first_name = first_name || 'z' "
            "WHERE id = %s",
            [row[0]],
        )

    def restore() -> None:
        with _admin(name) as admin:
            admin.execute(
                "UPDATE clinic_app.identity_user SET first_name = %s WHERE id = %s",
                [row[1], row[0]],
            )

    return restore


def _table(name: str) -> Callable[[], None]:
    with _admin(name) as admin:
        admin.execute("CREATE TABLE clinic_app.zz_stale_world (id integer)")

    def restore() -> None:
        with _admin(name) as admin:
            admin.execute("DROP TABLE clinic_app.zz_stale_world")

    return restore


def _acl(name: str) -> Callable[[], None]:
    grant = sql.SQL("{} TEMPORARY ON DATABASE {} {} clinic_app")
    with _admin("postgres") as admin:
        admin.execute(
            grant.format(sql.SQL("GRANT"), sql.Identifier(name), sql.SQL("TO"))
        )

    def restore() -> None:
        with _admin("postgres") as admin:
            admin.execute(
                grant.format(sql.SQL("REVOKE"), sql.Identifier(name), sql.SQL("FROM"))
            )

    return restore


@pytest.mark.parametrize(
    ("alter", "match"),
    [(_row, "content"), (_table, "content"), (_acl, "environment")],
    ids=["row", "table", "database-acl"],
)
def test_a_stale_template_is_refused(
    seeded_world: probe_worlds.SeededWorld,
    alter: Callable[[str], Callable[[], None]],
    match: str,
) -> None:
    with override_settings(**probe_worlds.SYNTHETIC):
        seeded_world()
        held = probe_worlds.template_now()
        restore = alter(held.name)
        try:
            with pytest.raises(
                probe_worlds.StaleWorldError,
                match=f"stale template {held.name}: its {match} changed",
            ):
                seeded_world()
        finally:
            restore()
        # The check is exact: the restored template is served again.
        world = seeded_world()
    assert world.matrix.states == held.world.matrix.states
    assert world is not held.world
    assert world.matrix.applied is not held.world.matrix.applied


def test_each_test_gets_its_own_clone_and_leaves_none(
    seeded_world: probe_worlds.SeededWorld,
) -> None:
    with override_settings(**probe_worlds.SYNTHETIC):
        seeded_world()
        first = str(connection.settings_dict["NAME"])
        seeded_world()
        second = str(connection.settings_dict["NAME"])
    assert first != second
    assert {first, second} == set(probe_worlds.leftovers())
    assert all(name.startswith(probe_worlds.CLONE_PREFIX) for name in (first, second))


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
