"""The scheduling template is seeded once, exact, disposable and fail-closed."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from django.db import connection, connections

from . import clock_catalog_cache
from .clock_catalog import live_clock_inventory
from .world_probe_support import unsealed
from .worlds import StaleWorldError

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

    from .worlds import Worlds

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("corruption", ["row", "catalog", "secret"])
def test_stale_scheduling_template_is_refused(
    rbac_graph: RbacGraph,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    corruption: str,
) -> None:
    held = scheduling_worlds.template
    assert held is not None
    original_secret = held.secret.read_bytes()
    with unsealed(held.name) as admin:
        if corruption == "row":
            admin.execute(
                "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
                [rbac_graph.shared_user],
            )
        elif corruption == "catalog":
            admin.execute(
                "ALTER TABLE clinic_app.identity_user "
                "ADD COLUMN stale_world_probe integer"
            )
        else:
            held.secret.write_bytes(b"invalid synthetic key")
    try:
        with (
            pytest.raises(StaleWorldError),
            scheduling_worlds.world(synthetic_secret_backend),
        ):
            pytest.fail("stale world was yielded")
    finally:
        with unsealed(held.name) as admin:
            if corruption == "row":
                admin.execute(
                    "UPDATE clinic_app.identity_user SET is_active=true WHERE id=%s",
                    [rbac_graph.shared_user],
                )
            elif corruption == "catalog":
                admin.execute(
                    "ALTER TABLE clinic_app.identity_user DROP COLUMN stale_world_probe"
                )
            else:
                held.secret.write_bytes(original_secret)
    with scheduling_worlds.world(synthetic_secret_backend) as restored:
        assert restored == rbac_graph
    assert scheduling_worlds.seed_count == 1


def test_worker_thread_uses_its_world_database(rbac_graph: RbacGraph) -> None:
    database = connection.settings_dict["NAME"]

    def worker() -> tuple[str, bool]:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), EXISTS(SELECT 1 FROM "
                    "clinic_app.identity_user WHERE id=%s)",
                    [rbac_graph.shared_user],
                )
                row = cursor.fetchone()
                assert row is not None
                return str(row[0]), bool(row[1])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(worker).result(timeout=10) == (database, True)


def test_equal_world_clones_share_catalog_closure(
    rbac_graph: RbacGraph,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
) -> None:
    with scheduling_worlds.world(synthetic_secret_backend) as first:
        assert first == rbac_graph
        expected = live_clock_inventory()
        builds = clock_catalog_cache.CACHE_STATS["builds"]
    with scheduling_worlds.world(synthetic_secret_backend) as second:
        assert second == rbac_graph
        assert live_clock_inventory() == expected
        assert clock_catalog_cache.CACHE_STATS["builds"] == builds
    assert scheduling_worlds.seed_count == 1
