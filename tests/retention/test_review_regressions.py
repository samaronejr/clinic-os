"""Source-only regressions for the rejected todo 43 review."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest
from apps.retention import tasks
from apps.retention.execution import RetentionExecutionDeniedError
from apps.retention.migrations._purge_execution_sql import SQL as EXECUTION_SQL
from apps.retention.migrations._purge_scope_sql import SQL as SCOPE_SQL
from config.celery import app
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.loader import MigrationLoader

if TYPE_CHECKING:
    from collections.abc import Callable


def test_beat_repeatedly_schedules_the_registered_retention_scanner() -> None:
    entries = [
        entry
        for entry in app.conf.beat_schedule.values()
        if entry["task"] == "apps.retention.tasks.scan_due"
    ]
    assert len(entries) == 1
    assert entries[0]["schedule"] == 60.0
    app.autodiscover_tasks(force=True)
    assert "apps.retention.tasks.scan_due" in app.tasks
    route = app.amqp.router.route({}, "apps.retention.tasks.scan_due")
    assert route["queue"].name == "bulk"


def test_repeated_scans_publish_only_registered_scopes_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, clinic = uuid4(), uuid4()
    agent = MagicMock()
    cursor = agent.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(principal, clinic)]
    callbacks: list[Callable[[], None]] = []

    def on_commit(callback: Callable[[], None], *, using: str) -> None:
        assert using == "agent"
        callbacks.append(callback)

    monkeypatch.setattr("apps.retention.tasks.connections", {"agent": agent})
    monkeypatch.setattr(
        "apps.retention.tasks.transaction", SimpleNamespace(on_commit=on_commit)
    )
    with patch("apps.retention.tasks.apply_policy.apply_async") as publish:
        for sweep in range(2):
            assert tasks.scan_due() == 3
            assert publish.call_count == sweep * 3
            for callback in callbacks:
                callback()
            callbacks.clear()
        expected = [
            call(
                kwargs={
                    "clinic_id": str(clinic),
                    "principal_id": str(principal),
                    "record_class": record_class,
                },
                queue="bulk",
            )
            for record_class in ("raw_audio", "ai_artifact", "transcript_partial")
        ]
        assert publish.call_args_list == expected * 2
    assert (
        cursor.execute.call_args_list
        == [call("SELECT * FROM clinic_app.retention_sweep_scopes()")] * 2
    )


def test_scanner_reuses_the_login_bound_v3_scope_resolver() -> None:
    function = SCOPE_SQL.partition(
        "CREATE FUNCTION clinic_app.retention_sweep_scopes()"
    )[2].partition("$f$;")[0]
    assert function
    assert "SECURITY DEFINER" in function
    assert "SET search_path=pg_catalog,clinic_app,pg_temp" in function
    assert (
        "clinic_app.retention_principal_scope(p.id,p.clinic_id)=p.organization_id"
        in function
    )


def test_scanner_cannot_open_a_live_execution_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.retention.tasks.resolved_data_mode", lambda: "live")
    with pytest.raises(RetentionExecutionDeniedError):
        tasks.scan_due()


def test_scanner_without_a_provisioned_agent_is_non_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.retention.tasks.connections", {})
    assert tasks.scan_due() == 0


@pytest.mark.parametrize("scan", [0, 1], ids=["dry-run-count", "receipt-dispatch"])
def test_held_prefix_cannot_consume_the_runnable_batch_limit(scan: int) -> None:
    # Source contract only: 100 held rows have their own bookkeeping batch,
    # so an unheld row behind them still enters the independent runnable batch.
    queries = re.findall(r"FOR candidate IN(.*?) LOOP", " ".join(EXECUTION_SQL.split()))
    assert len(queries) == 2
    cohorts = re.findall(
        r"SELECT \* FROM due_records WHERE (NOT )?held "
        r"ORDER BY expires,id LIMIT 100",
        queries[scan],
    )
    assert cohorts == ["NOT ", ""]
    assert (
        "SELECT * FROM runnable UNION ALL SELECT * FROM held_records" in queries[scan]
    )


def test_ai_protected_field_migration_is_non_atomic() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    migration = loader.get_migration("ai", "0003_artifact_retention")
    assert migration.atomic is False


def test_ai_protected_field_rollback_fails_before_any_database_mutation() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    migration = loader.get_migration("ai", "0003_artifact_retention")
    editor = MagicMock()

    with pytest.raises(IrreversibleError):
        migration.unapply(loader.project_state([("ai", "0001_initial")]), editor)

    assert editor.mock_calls == []
