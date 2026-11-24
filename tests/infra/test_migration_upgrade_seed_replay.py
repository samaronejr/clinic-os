"""A disposable upgrade database must never resume a previous fixture load."""

from __future__ import annotations

import json
import os
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import connection
from ops.testing.migration_upgrade_rows import differences, read_rows, table_keys
from ops.testing.migration_upgrade_seed_state import (
    observe_base,
    require_completed_seed,
)
from psycopg import Rollback, sql

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def seed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    superuser_database_url: str,
    synthetic_secret_backend: Path,
) -> Iterator[Path]:
    receipt = tmp_path / "seed.json"
    monkeypatch.setenv("UPGRADE_SEED_FILE", str(receipt))
    monkeypatch.setenv("UPGRADE_HELPER_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("UPGRADE_BASE_OBSERVATION", str(tmp_path / "base.json"))
    monkeypatch.setenv("UPGRADE_BASE_SHA", "synthetic-base")
    monkeypatch.setenv("UPGRADE_ADMIN_DATABASE_URL", superuser_database_url)
    observe_base()
    try:
        yield receipt
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS upgrade_rehearsal")


def _run_seed() -> None:
    run_path(str(PROJECT_ROOT / "ops/testing/migration_upgrade_seed.py"))


def _counts(admin_dsn: str) -> tuple[int, int, int]:
    with psycopg.connect(admin_dsn) as admin, admin.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.identity_organization), "
            "(SELECT count(*) FROM clinic_app.ai_aiinvocation), "
            "(SELECT count(*) FROM clinic_app.audit_event)"
        )
        row = cursor.fetchone()
        assert row is not None
        return row[0], row[1], row[2]


@pytest.mark.django_db(transaction=True)
def test_publication_failure_requires_fresh_database(
    seed_receipt: Path, superuser_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_replace = Path.replace

    def fail_publication(path: Path, target: str | os.PathLike[str]) -> Path:
        if target == seed_receipt:
            raise OSError
        return original_replace(path, target)

    before = _counts(superuser_database_url)
    with monkeypatch.context() as publication:
        publication.setattr(Path, "replace", fail_publication)
        with pytest.raises(SystemExit) as failed:
            _run_seed()
    assert failed.value.code == 74
    assert not seed_receipt.exists()
    seed_receipt.with_suffix(".pending").unlink()
    committed = _counts(superuser_database_url)
    assert committed == (before[0] + 1, before[1] + 14, before[2] + 5)
    with pytest.raises(RuntimeError, match="partial seed: rebuild database"):
        _run_seed()
    assert not seed_receipt.exists()
    assert _counts(superuser_database_url) == committed
    Path(os.environ["UPGRADE_BASE_OBSERVATION"]).unlink()
    with pytest.raises(RuntimeError, match="partial seed: rebuild database"):
        observe_base()


@pytest.mark.django_db(transaction=True)
def test_foreign_database_checkpoint_is_refused(seed_receipt: Path) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA upgrade_rehearsal")
    try:
        with pytest.raises(RuntimeError, match="partial seed: rebuild database"):
            _run_seed()
        assert not seed_receipt.exists()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA upgrade_rehearsal")


@pytest.mark.django_db(transaction=True)
def test_changed_or_partial_fixture_cannot_seed_even_without_file_markers(
    seed_receipt: Path, superuser_database_url: str
) -> None:
    _run_seed()
    contents = seed_receipt.read_text()
    assert require_completed_seed(contents) == ["core", "ai"]
    markers = json.loads(contents)
    markers.pop("ai")
    markers["ai_present"] = False
    with pytest.raises(RuntimeError, match="upgrade seed coverage mismatch"):
        require_completed_seed(json.dumps(markers))
    seed_receipt.unlink()
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:
        admin.execute(
            "UPDATE clinic_app.scheduling_availabilityblock "
            "SET retired_at=clock_timestamp() WHERE id=%s",
            [markers["availability_id"]],
        )
        with pytest.raises(RuntimeError, match="partial seed: rebuild database"):
            _run_seed()
        admin.execute(
            "ALTER TABLE clinic_app.ai_aiinvocation RENAME TO original_invocation"
        )
        try:
            admin.execute(
                "CREATE TABLE clinic_app.ai_aiinvocation "
                "(LIKE clinic_app.original_invocation)"
            )
            with pytest.raises(RuntimeError, match="partial seed: rebuild database"):
                _run_seed()
        finally:
            admin.execute("DROP TABLE IF EXISTS clinic_app.ai_aiinvocation")
            admin.execute(
                "ALTER TABLE clinic_app.original_invocation RENAME TO ai_aiinvocation"
            )
    assert not seed_receipt.exists()


@pytest.mark.django_db(transaction=True)
def test_every_seeded_table_rejects_a_forward_row_mutation(
    seed_receipt: Path, superuser_database_url: str
) -> None:
    _run_seed()
    with psycopg.connect(superuser_database_url) as admin, admin.cursor() as cursor:
        expected = read_rows(cursor)
        keys = dict(table_keys(cursor))
        for table, rows in expected.items():
            key = next(iter(rows))
            column = next(
                col
                for col, value in sorted(rows[key].items())
                if col not in keys[table] and value is not None
            )
            original = sql.Identifier("clinic_app", table)
            copy_name = sql.Identifier("fixture_sweep_original")
            # Real SQL values, with a rollback-only shadow table so fixture
            # constraints do not mask the comparator's independent rejection.
            with admin.transaction() as mutation:
                cursor.execute(
                    sql.SQL("ALTER TABLE {} RENAME TO {}").format(original, copy_name)
                )
                cursor.execute(
                    sql.SQL("CREATE TABLE {} AS SELECT * FROM clinic_app.{}").format(
                        original, copy_name
                    )
                )
                cursor.execute(
                    sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
                        original, sql.SQL(", ").join(map(sql.Identifier, keys[table]))
                    )
                )
                cursor.execute(
                    sql.SQL("UPDATE {} AS t SET {}=NULL WHERE {}").format(
                        original,
                        sql.Identifier(column),
                        sql.SQL(" AND ").join(
                            sql.SQL("to_jsonb(t)->{} = %s::jsonb").format(
                                sql.Literal(name)
                            )
                            for name in keys[table]
                        ),
                    ),
                    list(map(json.dumps, json.loads(key))),
                )
                assert cursor.rowcount == 1
                actual = read_rows(cursor)
                actual.pop("fixture_sweep_original")
                assert differences(expected, actual) == [
                    f"seeded value mismatch: {table}.{column} {key}"
                ]
                raise Rollback(mutation)
