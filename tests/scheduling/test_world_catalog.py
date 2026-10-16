"""Every omitted world-catalog class must demonstrate a served stale clone."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import TYPE_CHECKING

import psycopg
import psycopg.sql
import pytest
from django.db import connection

from . import clock_catalog_cache
from .world_database import admin_url
from .world_lock import connect_grants
from .world_probe_support import plant, unsealed
from .worlds import StaleWorldError

if TYPE_CHECKING:
    from pathlib import Path

    from .worlds import Worlds

pytestmark = pytest.mark.django_db(transaction=True, available_apps=[])
CLASSES = ("column-acl", "default-acl", "cast", "operator", "sequence")
RESTORATION = pytest.StashKey[dict[str, str]]()


@pytest.fixture(autouse=True)
def seed_catalog_template(
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    _django_db_helper: None,
) -> None:
    # These tests exercise world() themselves. An additional autouse RBAC clone
    # only duplicated that isolation boundary and is unnecessary for the oracle.
    scheduling_worlds.seeded(synthetic_secret_backend)


def fingerprint(admin: psycopg.Connection[tuple[object, ...]], query: str) -> str:
    # The same administrative role/query reads both complete catalog snapshots.
    # No extra clone or seal/unseal round trip is needed to prove restoration.
    row = admin.execute(query.encode(), ["{}"]).fetchone()
    assert row is not None
    assert isinstance(row[0], bytes)
    return hashlib.sha256(row[0]).hexdigest()


def test_template_is_sealed(scheduling_worlds: Worlds) -> None:
    held = scheduling_worlds.template
    assert held is not None
    assert connect_grants(held.name) == ()
    with pytest.raises(
        psycopg.OperationalError, match="not currently accepting connections"
    ):
        psycopg.connect(admin_url(held.name)).close()


@pytest.mark.parametrize("kind", CLASSES)
@pytest.mark.parametrize("variant", [0, 1])
def test_world_metadata_class_is_refused(
    kind: str,
    variant: int,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    request: pytest.FixtureRequest,
) -> None:
    held = scheduling_worlds.template
    assert held is not None
    install, remove = plant(held.name, kind, variant)
    with unsealed(held.name) as admin:
        before = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
        admin.execute(install)
    try:
        with (
            pytest.raises(StaleWorldError),
            scheduling_worlds.world(synthetic_secret_backend),
        ):
            pytest.fail("stale catalog was served")
    finally:
        with unsealed(held.name) as admin:
            admin.execute(remove)
            after = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
    assert before == after
    request.node.stash[RESTORATION] = {
        "class": kind,
        "catalog_before_sha256": before,
        "catalog_after_sha256": after,
        "stale_refused": "true",
    }


@pytest.mark.parametrize("kind", CLASSES)
def test_omitting_world_class_serves_its_plant(
    kind: str,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    held = scheduling_worlds.template
    assert held is not None
    install, remove = plant(held.name, kind, 0)
    complete = clock_catalog_cache.CATALOG_VERSION
    # Column grants now live in the complete pg_attribute row, not a
    # separately selected ACL projection. Dropping that class must still
    # demonstrate the original column-grant defect by serving its plant.
    catalog_class = "attribute" if kind == "column-acl" else kind
    dropped, count = re.subn(
        r" UNION ALL SELECT '" + catalog_class + r"',.*?(?=\n UNION ALL|\n\))",
        "",
        complete,
        flags=re.DOTALL,
    )
    assert count == 1
    if kind == "operator":
        # The full dependency census also records an operator's namespace edge.
        # This mutant removes the entire operator class, including its edges.
        dropped = dropped.replace(
            "SELECT 'dependency',ROW(x.*)::text FROM pg_depend x",
            "SELECT 'dependency',ROW(x.*)::text FROM pg_depend x "
            "WHERE x.classid<>'pg_operator'::regclass",
        )
    with (
        scheduling_worlds.world(synthetic_secret_backend),
        monkeypatch.context() as patch,
    ):
        complete_before = clock_catalog_cache.catalog_version()
        assert complete_before == held.catalog
        patch.setattr(clock_catalog_cache, "CATALOG_VERSION", dropped)
        incomplete = clock_catalog_cache.catalog_version()
    with monkeypatch.context() as patch:
        patch.setattr(clock_catalog_cache, "CATALOG_VERSION", dropped)
        patch.setattr(scheduling_worlds, "template", replace(held, catalog=incomplete))
        with unsealed(held.name) as admin:
            before = fingerprint(admin, complete)
            admin.execute(install)
        try:
            with scheduling_worlds.world(synthetic_secret_backend) as served:
                assert served == held.graph
                with monkeypatch.context() as verify:
                    verify.setattr(clock_catalog_cache, "CATALOG_VERSION", complete)
                    assert clock_catalog_cache.catalog_version() != complete_before
                # Acceptance is the defect demonstrated by this process-local mutant.
        finally:
            with unsealed(held.name) as admin:
                admin.execute(remove)
                after = fingerprint(admin, complete)
    assert before == after
    request.node.stash[RESTORATION] = {
        "class": kind,
        "catalog_before_sha256": before,
        "catalog_after_sha256": after,
        "mutant_served_stale": "true",
    }


def test_database_acl_is_hashed_as_a_set_not_an_order() -> None:
    """Clones regain CONNECT in role-name order; the template kept OID order.

    Reordering the same grants must not make a clone look stale, and any real
    grant change must still change the catalog version.
    """
    database = str(connection.settings_dict["NAME"])
    grants = connect_grants(database)
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        # The first CONNECT grantee in array order: re-granting moves it last.
        row = admin.execute(
            "SELECT pg_get_userbyid(a.grantee), a.is_grantable "
            "FROM pg_database d, unnest(d.datacl) WITH ORDINALITY i(item, position), "
            "aclexplode(ARRAY[i.item]) a WHERE d.datname=%s AND a.grantee<>0 "
            "AND a.grantee<>d.datdba AND a.privilege_type='CONNECT' "
            "ORDER BY i.position LIMIT 1",
            [database],
        ).fetchone()
    assert row is not None
    role, grantable = str(row[0]), bool(row[1])
    grantee = psycopg.sql.Identifier(role)
    target = psycopg.sql.Identifier(database)
    option = psycopg.sql.SQL(" WITH GRANT OPTION" if grantable else "")
    before = clock_catalog_cache.catalog_version()
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        acl = "SELECT datacl::text FROM pg_database WHERE datname=%s"
        (original,) = admin.execute(acl, [database]).fetchone() or ("",)
        try:
            # Same grant, moved to the end of the ACL array.
            admin.execute(
                psycopg.sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    target, grantee
                )
            )
            admin.execute(
                psycopg.sql.SQL("GRANT CONNECT ON DATABASE {} TO {}{}").format(
                    target, grantee, option
                )
            )
            (reordered,) = admin.execute(acl, [database]).fetchone() or ("",)
            assert reordered != original
            assert clock_catalog_cache.catalog_version() == before
            # A real grant change is catalog state.
            admin.execute(
                psycopg.sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}").format(
                    target, grantee
                )
            )
            assert clock_catalog_cache.catalog_version() != before
            admin.execute(
                psycopg.sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(
                    target, grantee
                )
            )
            assert clock_catalog_cache.catalog_version() == before
        finally:
            admin.execute(
                psycopg.sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(
                    target, grantee
                )
            )
    assert set(connect_grants(database)) == set(grants)
