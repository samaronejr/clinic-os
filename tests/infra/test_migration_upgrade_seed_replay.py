"""A disposable upgrade database must never resume a previous fixture load."""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.intake import patient_creation, patient_name_index
from django.db import connection
from ops.testing.migration_upgrade_ai_invocation_verify import verify_invocation_fixture
from ops.testing.migration_upgrade_core_verify import verify_core_fixture
from ops.testing.migration_upgrade_rows import (
    differences,
    expected_rows,
    read_rows,
    table_keys,
)
from ops.testing.migration_upgrade_seed_state import (
    observe_base,
    require_completed_seed,
)
from psycopg import Rollback, sql

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Cursor


def _catalog_counts(cursor: Cursor[tuple[object, ...]]) -> dict[str, int]:
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='clinic_app' AND table_type='BASE TABLE'"
    )
    tables = [str(row[0]) for row in cursor.fetchall()]
    counts = {}
    for table in tables:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier("clinic_app", table)
            )
        )
        row = cursor.fetchone()
        assert row is not None
        assert isinstance(row[0], int)
        counts[table] = row[0]
    return counts


def _assert_sweep_coverage(seeded: set[str], swept: set[str]) -> None:
    assert seeded == swept, f"sweep coverage mismatch: {sorted(seeded ^ swept)}"


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
    monkeypatch.setenv("CLINIC_SECRET_DIR", str(synthetic_secret_backend))
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
    with psycopg.connect(superuser_database_url) as admin, admin.cursor() as cursor:
        before = _catalog_counts(cursor)
        _run_seed()
        after = _catalog_counts(cursor)
        seeded = {name for name in after if after[name] > before[name]}
        expected = read_rows(cursor)
        _assert_sweep_coverage(seeded, set(expected))
        for omitted in seeded:
            with pytest.raises(
                AssertionError, match=f"sweep coverage mismatch:.*{omitted}"
            ):
                _assert_sweep_coverage(seeded, set(expected) - {omitted})
        swept = set()
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
                swept.add(table)
                raise Rollback(mutation)
        _assert_sweep_coverage(seeded, swept)


@pytest.mark.django_db(transaction=True)
def test_typed_comparator_rejects_changed_array_lower_bound(
    seed_receipt: Path, superuser_database_url: str
) -> None:
    _run_seed()
    with psycopg.connect(superuser_database_url) as admin, admin.cursor() as cursor:
        expected = read_rows(cursor, typed=True)
        with admin.transaction() as mutation:
            cursor.execute(
                "UPDATE clinic_app.intake_patientaccessgrant "
                "SET operations=('[0:' || (cardinality(operations)-1) || ']=' "
                "|| operations::text)::varchar[]"
            )
            assert cursor.rowcount == 1
            failures = differences(expected, read_rows(cursor, typed=True))
            assert len(failures) == 1
            assert failures[0].startswith(
                "seeded value mismatch: intake_patientaccessgrant.operations "
            )
            raise Rollback(mutation)


@pytest.mark.django_db(transaction=True)
def test_core_oracle_rejects_wrong_values_without_a_capture(
    seed_receipt: Path, superuser_database_url: str
) -> None:
    _run_seed()
    assert verify_core_fixture() == []
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:
        admin.execute(
            "UPDATE clinic_app.identity_organization "
            "SET name='Unexpected Synthetic Organization'"
        )
        assert verify_core_fixture() == [
            "core fixture mismatch: identity_organization.name"
        ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("statement", "failure"),
    [
        (
            "UPDATE clinic_app.ai_aiinvocation SET configuration_id="
            "(SELECT id FROM clinic_app.ai_modelconfiguration WHERE purpose='analyst') "
            "WHERE purpose='front_desk' AND status='reserved'",
            "AI invocation fixture: foreign-key target ('front_desk', 'reserved')",
        ),
        (
            "UPDATE clinic_app.ai_aiartifact a SET content=( "
            " SELECT b.content FROM clinic_app.ai_aiartifact b "
            " JOIN clinic_app.ai_aiinvocation j ON j.inputs_ref_id=b.id "
            " WHERE j.purpose='front_desk' AND j.status<>i.status) "
            "FROM clinic_app.ai_aiinvocation i "
            "WHERE i.purpose='front_desk' AND i.inputs_ref_id=a.id",
            "AI invocation fixture: ciphertext ('front_desk', 'reserved')",
        ),
        (
            "UPDATE clinic_app.ai_aiartifact a SET created_at=i.created_at "
            "+ interval '1 second' FROM clinic_app.ai_aiinvocation i "
            "WHERE i.purpose='front_desk' AND i.status='reserved' "
            "AND i.inputs_ref_id=a.id",
            "AI invocation fixture: timestamp order ('front_desk', 'reserved')",
        ),
    ],
)
def test_ai_invocation_oracle_detects_wrong_generated_values(
    seed_receipt: Path, superuser_database_url: str, statement: str, failure: str
) -> None:
    _run_seed()
    assert verify_invocation_fixture() == []
    with psycopg.connect(superuser_database_url) as admin:
        admin.execute("SET LOCAL session_replication_role = replica")
        result = admin.execute(statement)
        assert result.rowcount in (1, 2)
    assert failure in verify_invocation_fixture()


@pytest.mark.django_db(transaction=True)
def test_blind_index_oracle_rejects_shared_writer_bug(
    seed_receipt: Path, superuser_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = patient_name_index.name_indexes

    def reverse_digests(name: str) -> tuple[bytes, ...]:
        return tuple(digest[::-1] for digest in original(name))

    backfill = import_module("apps.intake.migrations.0012_patient_name_index")
    monkeypatch.setattr(patient_name_index, "name_indexes", reverse_digests)
    monkeypatch.setattr(patient_creation, "name_indexes", reverse_digests)
    monkeypatch.setattr(backfill, "name_indexes", reverse_digests)
    _run_seed()
    with psycopg.connect(superuser_database_url) as admin, admin.cursor() as cursor:
        actual = {"intake_patient": read_rows(cursor)["intake_patient"]}
    base_rows = {
        "intake_patient": {
            key: {
                column: value
                for column, value in row.items()
                if column != "full_name_index"
            }
            for key, row in actual["intake_patient"].items()
        }
    }
    expected = expected_rows(base_rows, {("intake", "0012_patient_name_index")})
    assert differences(expected, actual) == [
        f"seeded value mismatch: intake_patient.full_name_index {key}"
        for key in actual["intake_patient"]
    ]
