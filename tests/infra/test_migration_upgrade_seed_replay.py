"""Exercise the real upgrade seed and replay it against the same PostgreSQL DB."""

from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import connection
from ops.testing.migration_upgrade_seed_state import require_completed_seed

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def seed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, synthetic_secret_backend: Path
) -> Iterator[Path]:
    receipt = tmp_path / "seed.json"
    monkeypatch.setenv("UPGRADE_SEED_FILE", str(receipt))
    monkeypatch.setenv("UPGRADE_HELPER_ROOT", str(PROJECT_ROOT))
    try:
        yield receipt
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS upgrade_rehearsal.seed")
            cursor.execute("DROP SCHEMA IF EXISTS upgrade_rehearsal")


def _run_seed() -> None:
    run_path(str(PROJECT_ROOT / "ops/testing/migration_upgrade_seed.py"))


def _counts(superuser_database_url: str) -> tuple[int, int, int]:
    with psycopg.connect(superuser_database_url) as admin, admin.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.identity_organization), "
            "(SELECT count(*) FROM clinic_app.ai_aiinvocation), "
            "(SELECT count(*) FROM clinic_app.audit_event)"
        )
        row = cursor.fetchone()
        assert row is not None
        organizations, invocations, audit_events = row
    return organizations, invocations, audit_events


@pytest.mark.django_db(transaction=True)
def test_retry_recovers_committed_seed_after_receipt_io_failure(
    seed_receipt: Path, superuser_database_url: str
) -> None:
    before = _counts(superuser_database_url)
    pending = seed_receipt.with_suffix(".pending")
    pending.mkdir()
    with pytest.raises(IsADirectoryError):
        _run_seed()
    assert not seed_receipt.exists()
    committed = _counts(superuser_database_url)
    assert committed == (before[0] + 1, before[1] + 14, before[2] + 5)

    pending.rmdir()
    with pytest.raises(SystemExit) as retry:
        _run_seed()
    assert retry.value.code == 0
    assert _counts(superuser_database_url) == committed
    contents = seed_receipt.read_text()
    assert require_completed_seed(contents) == ["core", "ai"]

    with pytest.raises(SystemExit) as replay:
        _run_seed()
    assert replay.value.code == 0
    assert seed_receipt.read_text() == contents
    assert _counts(superuser_database_url) == committed

    markers = json.loads(contents)
    markers.pop("ai")
    markers["ai_present"] = False
    with pytest.raises(RuntimeError, match="upgrade seed coverage mismatch"):
        require_completed_seed(json.dumps(markers))
