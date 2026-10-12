"""Whole catalog rows reject semantic drift, but maintenance remains harmless."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from . import clock_catalog_cache
from .test_world_catalog import fingerprint, seed_catalog_template
from .world_probe_support import unsealed
from .worlds import StaleWorldError

if TYPE_CHECKING:
    from pathlib import Path

    from .worlds import Worlds

__all__ = ["seed_catalog_template"]

pytestmark = pytest.mark.django_db(transaction=True, available_apps=[])
RESTORATION = pytest.StashKey[dict[str, str]]()
PLANTS = {
    "not-null": (
        "ALTER TABLE clinic_app.scheduling_resource ALTER COLUMN name DROP NOT NULL",
        "ALTER TABLE clinic_app.scheduling_resource ALTER COLUMN name SET NOT NULL",
    ),
    "strict": (
        "ALTER FUNCTION clinic_app.scheduling_definition_guard() STRICT",
        "ALTER FUNCTION clinic_app.scheduling_definition_guard() CALLED ON NULL INPUT",
    ),
    "leakproof": (
        "ALTER FUNCTION clinic_app.scheduling_definition_guard() LEAKPROOF",
        "ALTER FUNCTION clinic_app.scheduling_definition_guard() NOT LEAKPROOF",
    ),
    "collation": (
        "ALTER TABLE clinic_app.scheduling_resource ALTER COLUMN name "
        'TYPE varchar(120) COLLATE "C"',
        "ALTER TABLE clinic_app.scheduling_resource ALTER COLUMN name "
        'TYPE varchar(120) COLLATE "default"',
    ),
    "replica-identity": (
        "ALTER TABLE clinic_app.scheduling_resource REPLICA IDENTITY FULL",
        "ALTER TABLE clinic_app.scheduling_resource REPLICA IDENTITY DEFAULT",
    ),
}


@pytest.mark.parametrize("name", PLANTS)
def test_whole_catalog_row_drift_is_refused(
    name: str,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    request: pytest.FixtureRequest,
) -> None:
    held = scheduling_worlds.template
    assert held is not None
    install, remove = PLANTS[name]
    with unsealed(held.name) as admin:
        before = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
        admin.execute(install.encode())
    try:
        with (
            pytest.raises(StaleWorldError),
            scheduling_worlds.world(synthetic_secret_backend),
        ):
            pytest.fail("a changed catalog row was served")
    finally:
        with unsealed(held.name) as admin:
            admin.execute(remove.encode())
            after = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
    assert before == after
    request.node.stash[RESTORATION] = {
        "catalog_before_sha256": before,
        "catalog_after_sha256": after,
        "stale_refused": "true",
        "column_class": name,
    }


@pytest.mark.parametrize("maintenance", ["ANALYZE", "VACUUM (FREEZE, ANALYZE)"])
def test_maintenance_is_not_semantic_drift(
    maintenance: str,
    scheduling_worlds: Worlds,
    synthetic_secret_backend: Path,
    request: pytest.FixtureRequest,
) -> None:
    held = scheduling_worlds.template
    assert held is not None
    with unsealed(held.name) as admin:
        before = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
        admin.execute(maintenance.encode())
        after = fingerprint(admin, clock_catalog_cache.CATALOG_VERSION)
    assert before == after
    with scheduling_worlds.world(synthetic_secret_backend) as world:
        assert world == held.graph
    request.node.stash[RESTORATION] = {
        "catalog_before_sha256": before,
        "catalog_after_sha256": after,
        "maintenance_accepted": maintenance,
    }
