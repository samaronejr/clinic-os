"""Session sharing must preserve fresh exhaustive catalog and source observations."""

from __future__ import annotations

import inspect
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.migrations._resumable_schema import TransactionScript
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from psycopg import sql

from . import clock_catalog_cache as cache
from . import clock_runsql
from .clock_catalog import live_clock_inventory
from .clock_runsql import SQLDecoder, run_sql_operations
from .clock_source import parsed_source
from .clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from django.db.migrations import Migration

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


def test_intake_dependency_clocks_use_the_authority_axis(
    superuser_database_url: str,
) -> None:
    reference = datetime(2035, 6, 1, tzinfo=UTC)
    ambient = datetime(2045, 7, 2, tzinfo=UTC)
    with (
        frozen_sql_clocks(superuser_database_url, reference, ambient) as authority,
        connection.cursor() as cursor,
    ):
        for signature in (
            "intake_patientdemographics_version_guard_v1()",
            "intake_correction_guard_v1()",
            "intake_patient_mirror_guard_v1()",
        ):
            cursor.execute(
                "SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure",
                [f"clinic_app.{signature}"],
            )
            row = cursor.fetchone()
            assert row is not None
            expression = re.search(
                r"COALESCE\([^;\n]*transaction_timestamp\(\)\)\)", row[0]
            )
            assert expression is not None, signature
            cursor.execute(sql.SQL("SELECT {}").format(sql.SQL(expression.group())))
            assert cursor.fetchone() == (authority,)
            assert authority not in (reference, ambient)


def _strings(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    assert isinstance(value, (tuple, list))
    return [item[0] if isinstance(item, (tuple, list)) else item for item in value]


def _assert_migration_sql(migration: Migration, path: Path, observed: list[str]) -> int:
    expected = [
        text
        for operation in run_sql_operations(migration.operations)
        for value in (operation.sql, operation.reverse_sql)
        for text in _strings(value)
    ]
    assert Counter(text for text in observed if text) == Counter(expected), path
    return len(expected)


@pytest.mark.django_db(transaction=True)
def test_finite_decoder_matches_every_shipped_migration_runsql_value() -> None:
    decoder = SQLDecoder(ROOT)
    checked = 0
    for migration in MigrationLoader(None).disk_migrations.values():
        path = Path(inspect.getfile(type(migration)))
        if not path.is_relative_to(ROOT / "apps"):
            continue
        observed = decoder.fragments(path, parsed_source(path.read_text()))
        checked += _assert_migration_sql(migration, path, observed)
    assert checked > 0


def test_transaction_script_cannot_disappear_from_finite_census(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoder = SQLDecoder(ROOT)
    qualified_name = f"{TransactionScript.__module__}.{TransactionScript.__name__}"
    without_script = clock_runsql._graph_constructors() - {qualified_name}
    graph = MigrationLoader(None, replace_migrations=False).graph
    checked = 0
    for migration in graph.nodes.values():
        if not any(
            isinstance(operation, TransactionScript)
            for operation in run_sql_operations(migration.operations)
        ):
            continue
        path = Path(inspect.getfile(type(migration)))
        tree = parsed_source(path.read_text())
        _assert_migration_sql(migration, path, decoder.fragments(path, tree))
        with monkeypatch.context() as mutant:
            mutant.setattr(clock_runsql, "_graph_constructors", lambda: without_script)
            incomplete = decoder.fragments(path, tree)
        with pytest.raises(AssertionError, match=re.escape(str(path))):
            _assert_migration_sql(migration, path, incomplete)
        checked += 1
    assert checked > 0
