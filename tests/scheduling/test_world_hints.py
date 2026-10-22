"""False catalog hints must not suppress real objects in a served world."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from . import clock_catalog_cache
from .test_world_catalog import fingerprint
from .world_database import admin_url
from .world_probe_support import unsealed
from .worlds import StaleWorldError, Worlds

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.django_db(transaction=True, available_apps=[])
RESTORATION = pytest.StashKey[dict[str, str]]()
HINTS = ("relhastriggers", "relhasindex", "relhasrules", "relhassubclass")
RESOURCE = "clinic_app.scheduling_resource"


@pytest.fixture(scope="module")
def hint_factory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worlds]:
    factory = Worlds(tmp_path_factory.mktemp("hint-worlds"))
    try:
        yield factory
    finally:
        factory.close()


def seed_hints(factory: Worlds, secret_root: Path) -> None:
    # Prepare real objects before the ordinary factory captures and seals its
    # template. Restore the source DB immediately; no expected digest is faked.
    with psycopg.connect(
        admin_url(str(connection.settings_dict["NAME"])), autocommit=True
    ) as admin:
        before = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
        original = admin.execute(
            "SELECT relhastriggers,relhasindex,relhasrules,relhassubclass "
            "FROM pg_class WHERE oid=%s::regclass",
            [RESOURCE],
        ).fetchone()
        assert original is not None
        with admin.transaction():
            admin.execute("CREATE SCHEMA world_hint_probe")
            admin.execute(
                "CREATE TABLE world_hint_probe.child () "
                "INHERITS (clinic_app.scheduling_resource)"
            )
            admin.execute(
                "CREATE RULE world_hint_rule AS ON INSERT TO "
                "clinic_app.scheduling_resource DO ALSO NOTIFY world_hint_probe"
            )
            admin.execute("CREATE TABLE world_hint_probe.parent(v integer)")
            admin.execute(
                "CREATE TABLE world_hint_probe.removed_child () "
                "INHERITS (world_hint_probe.parent)"
            )
            admin.execute("DROP TABLE world_hint_probe.removed_child")
            admin.execute("CREATE VIEW world_hint_probe.viewed AS SELECT 1 AS v")
            admin.execute("GRANT USAGE ON SCHEMA world_hint_probe TO clinic_owner")
            admin.execute("GRANT SELECT ON world_hint_probe.viewed TO clinic_owner")
    try:
        factory.seeded(secret_root)
    finally:
        with psycopg.connect(
            admin_url(str(connection.settings_dict["NAME"])), autocommit=True
        ) as admin:
            admin.execute("DROP RULE world_hint_rule ON clinic_app.scheduling_resource")
            admin.execute("DROP SCHEMA world_hint_probe CASCADE")
            admin.execute(
                "UPDATE pg_class SET relhastriggers=%s,relhasindex=%s,"
                "relhasrules=%s,relhassubclass=%s WHERE oid=%s::regclass",
                [*original, RESOURCE],
            )
            assert fingerprint(admin, clock_catalog_cache.CATALOG_VERSION) == before


@pytest.fixture
def hint_world(
    hint_factory: Worlds,
    synthetic_secret_backend: Path,
    _django_db_helper: None,
    request: pytest.FixtureRequest,
) -> Iterator[Worlds]:
    if hint_factory.template is None:
        seed_hints(hint_factory, synthetic_secret_backend)
    held = hint_factory.template
    assert held is not None
    complete = clock_catalog_cache.CATALOG_VERSION
    with unsealed(held.name) as admin:
        before = fingerprint(admin, complete)
    try:
        yield hint_factory
    finally:
        with unsealed(held.name) as admin:
            after = fingerprint(admin, complete)
        assert before == after
        assert hint_factory.seed_count == 1
        receipt = request.node.stash.get(RESTORATION, {})
        request.node.stash[RESTORATION] = receipt | {
            "catalog_before_sha256": before,
            "catalog_after_sha256": after,
            "hint_factory_seeds": "1",
            "hint_factory_clones": str(hint_factory.clone_count),
        }


def set_hint(
    admin: psycopg.Connection[tuple[object, ...]], hint: str, *, value: bool
) -> None:
    admin.execute(
        sql.SQL("UPDATE pg_class SET {}=%s WHERE oid=%s::regclass").format(
            sql.Identifier(hint)
        ),
        [value, RESOURCE],
    )


@pytest.mark.parametrize("hint", HINTS)
def test_false_hint_with_objects_is_refused(
    hint: str,
    hint_world: Worlds,
    synthetic_secret_backend: Path,
    request: pytest.FixtureRequest,
) -> None:
    held = hint_world.template
    assert held is not None
    with unsealed(held.name) as admin:
        objects = admin.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=%s::regclass),"
            "EXISTS(SELECT 1 FROM pg_index WHERE indrelid=%s::regclass),"
            "EXISTS(SELECT 1 FROM pg_rewrite WHERE ev_class=%s::regclass "
            "AND rulename<>'_RETURN'),"
            "EXISTS(SELECT 1 FROM pg_inherits WHERE inhparent=%s::regclass)",
            [RESOURCE] * 4,
        ).fetchone()
        assert objects == (True, True, True, True)
        set_hint(admin, hint, value=False)
    try:
        with pytest.raises(StaleWorldError), hint_world.world(synthetic_secret_backend):
            pytest.fail("a hint suppressed real catalog objects in a served clone")
    finally:
        with unsealed(held.name) as admin:
            set_hint(admin, hint, value=True)
    request.node.stash[RESTORATION] = {"hint": hint, "stale_refused": "true"}


def invalid_resource_insert() -> None:
    with (
        psycopg.connect(admin_url(str(connection.settings_dict["NAME"]))) as admin,
        admin.transaction(force_rollback=True),
    ):
        admin.execute(
            "INSERT INTO clinic_app.scheduling_resource "
            "(id,kind,name,capacity,active,clinic_id,organization_id) "
            "VALUES (%s,'room','Synthetic hint probe',1,true,%s,%s)",
            [uuid4(), uuid4(), uuid4()],
        )
        admin.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_omitted_trigger_consistency_serves_disabled_enforcement(
    hint_world: Worlds,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    held = hint_world.template
    assert held is not None
    complete = clock_catalog_cache.CATALOG_VERSION
    term = (
        "c.relhastriggers OR NOT EXISTS "
        "(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid)"
    )
    assert complete.count(term) == 1
    omitted = complete.replace(term, "true")
    with hint_world.world(synthetic_secret_backend), monkeypatch.context() as patch:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            invalid_resource_insert()
        patch.setattr(clock_catalog_cache, "CATALOG_VERSION", omitted)
        incomplete = clock_catalog_cache.catalog_version()
    with monkeypatch.context() as patch:
        patch.setattr(clock_catalog_cache, "CATALOG_VERSION", omitted)
        patch.setattr(hint_world, "template", replace(held, catalog=incomplete))
        with unsealed(held.name) as admin:
            set_hint(admin, "relhastriggers", value=False)
        try:
            with hint_world.world(synthetic_secret_backend) as served:
                assert served == held.graph
                invalid_resource_insert()
                with monkeypatch.context() as verify:
                    verify.setattr(clock_catalog_cache, "CATALOG_VERSION", complete)
                    assert clock_catalog_cache.catalog_version() != held.catalog
        finally:
            with unsealed(held.name) as admin:
                set_hint(admin, "relhastriggers", value=True)
    request.node.stash[RESTORATION] = {
        "mutant_served_stale": "true",
        "invalid_insert_succeeded": "true",
    }


@pytest.mark.parametrize("maintenance", ["ANALYZE", "VACUUM (FREEZE, ANALYZE)"])
def test_lazy_hint_maintenance_is_accepted(
    maintenance: str,
    hint_world: Worlds,
    synthetic_secret_backend: Path,
    request: pytest.FixtureRequest,
) -> None:
    held = hint_world.template
    assert held is not None
    with unsealed(held.name) as admin:
        query = (
            "SELECT relhassubclass FROM pg_class "
            "WHERE oid='world_hint_probe.parent'::regclass"
        )
        admin.execute(
            "UPDATE pg_class SET relhassubclass=true "
            "WHERE oid='world_hint_probe.parent'::regclass"
        )
        assert admin.execute(query).fetchone() == (True,)
        before = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
        admin.execute(maintenance.encode())
        assert admin.execute(query).fetchone() == (False,)
        assert fingerprint(admin, clock_catalog_cache.CATALOG_VERSION) == before
    with hint_world.world(synthetic_secret_backend) as served:
        assert served == held.graph
    request.node.stash[RESTORATION] = {"maintenance_accepted": maintenance}


def test_view_return_rule_requires_consistent_hint(
    hint_world: Worlds,
    synthetic_secret_backend: Path,
) -> None:
    held = hint_world.template
    assert held is not None
    with hint_world.world(synthetic_secret_backend), connection.cursor() as cursor:
        cursor.execute("SELECT v FROM world_hint_probe.viewed")
        assert cursor.fetchone() == (1,)
    with unsealed(held.name) as admin:
        assert admin.execute(
            "SELECT rulename FROM pg_rewrite "
            "WHERE ev_class='world_hint_probe.viewed'::regclass"
        ).fetchall() == [("_RETURN",)]
        admin.execute(
            "UPDATE pg_class SET relhasrules=false "
            "WHERE oid='world_hint_probe.viewed'::regclass"
        )
    try:
        with pytest.raises(StaleWorldError), hint_world.world(synthetic_secret_backend):
            pytest.fail("a view's required return rule was suppressed")
    finally:
        with unsealed(held.name) as admin:
            admin.execute(
                "UPDATE pg_class SET relhasrules=true "
                "WHERE oid='world_hint_probe.viewed'::regclass"
            )
