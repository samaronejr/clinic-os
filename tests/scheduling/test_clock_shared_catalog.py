"""Session sharing must preserve fresh exhaustive catalog and source observations."""

from __future__ import annotations

import inspect
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader

from . import clock_catalog_cache as cache
from .clock_catalog import live_clock_inventory
from .clock_runsql import SQLDecoder
from .clock_source import parsed_source
from .clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.operations.base import Operation

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]
ROOT = Path(__file__).resolve().parents[2]


def test_unchanged_catalog_shares_the_complete_closure() -> None:
    expected = live_clock_inventory()
    builds = cache.CACHE_STATS["builds"]
    hits = cache.CACHE_STATS["hits"]
    assert live_clock_inventory() == expected
    assert cache.CACHE_STATS["builds"] == builds
    assert cache.CACHE_STATS["hits"] == hits + 1


def test_only_exact_fixture_definitions_share_across_reference_values(
    superuser_database_url: str,
) -> None:
    with frozen_sql_clocks(superuser_database_url, datetime(2035, 6, 1, tzinfo=UTC)):
        expected = live_clock_inventory()
        builds = cache.CACHE_STATS["builds"]
    with frozen_sql_clocks(superuser_database_url, datetime(2036, 7, 2, tzinfo=UTC)):
        assert live_clock_inventory() == expected
        assert cache.CACHE_STATS["builds"] == builds


def _operations(operations: Iterable[Operation]) -> Iterator[migrations.RunSQL]:
    for operation in operations:
        if isinstance(operation, migrations.RunSQL):
            yield operation
        elif isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _operations(operation.database_operations)


def _strings(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    assert isinstance(value, (tuple, list))
    return [item[0] if isinstance(item, (tuple, list)) else item for item in value]


@pytest.mark.django_db(transaction=True)
def test_finite_decoder_matches_every_shipped_migration_runsql_value() -> None:
    decoder = SQLDecoder(ROOT)
    checked = 0
    for migration in MigrationLoader(connection).disk_migrations.values():
        path = Path(inspect.getfile(type(migration)))
        if not path.is_relative_to(ROOT / "apps"):
            continue
        expected = [
            text
            for operation in _operations(migration.operations)
            for value in (operation.sql, operation.reverse_sql)
            for text in _strings(value)
        ]
        observed = decoder.fragments(path, parsed_source(path.read_text()))
        assert Counter(text for text in observed if text) == Counter(expected), path
        checked += len(expected)
    assert checked > 0
