"""Historical AI coverage cannot silently regress to an empty-table upgrade."""

from __future__ import annotations

import ast
import inspect
import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from django.apps import apps
from django.db import migrations, models
from ops.testing import migration_upgrade_ai_recipe as seed
from ops.testing import migration_upgrade_ai_verify as upgrade
from ops.testing import migration_upgrade_verdict as publisher
from ops.testing.migration_upgrade_verdict import completed_seeds

if TYPE_CHECKING:
    from pathlib import Path


def test_upgrade_requires_every_changed_model_and_sql_trigger_table() -> None:
    required = upgrade.altered_ai_tables()
    assert {
        "ai_gatewayswitch",
        "ai_modelconfiguration",
        "ai_modelchangerequest",
        "ai_evaluationrun",
        "ai_evaluationrating",
        "ai_aiinvocation",
        "ai_aiartifact",
        "ai_agentrun",
        "ai_actionproposal",
        "ai_actionexecution",
        "ai_frontdeskturn",
        "scribe_chunktranscription",
        "scribe_transcriptversion",
        "scribe_transcriptsegment",
        "scribe_aidraft",
        "scribe_draftsectionsuggestion",
        "scribe_reviewdecision",
        "ai_extractionjob",
        "ai_extractioncandidate",
        "orders_resultobservation",
        "ai_careproposal",
        "analytics_analystreview",
        "ai_approval",
    } <= required
    assert not upgrade.coverage_failures({table: {"count": 1} for table in required})
    for table in required:
        rows = {name: {"count": 1} for name in required}
        del rows[table]
        assert upgrade.coverage_failures(rows) == [
            "AI upgrade seed missing populated table: " + table
        ]
        rows[table] = {"count": 0}
        assert upgrade.coverage_failures(rows)


def _seed_tables(source: str) -> set[str]:
    """Read the actual fixture writes, never a second declared coverage list."""
    return {
        apps.get_model(node.args[0].value)._meta.db_table
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "make"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_real_seed_covers_migration_guard_tables() -> None:
    tables = _seed_tables(inspect.getsource(seed))
    assert not upgrade.coverage_failures({table: {"count": 1} for table in tables})


def test_removing_real_seed_write_is_detected() -> None:
    source = inspect.getsource(seed)
    mutated = source.replace('"ai.Approval"', '"ai.ActionProposal"')
    assert mutated != source
    assert upgrade.coverage_failures(
        {table: {"count": 1} for table in _seed_tables(mutated)}
    ) == ["AI upgrade seed missing populated table: ai_approval"]


def test_recreated_guard_finds_existing_nested_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = SimpleNamespace(
        operations=[
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    migrations.RunSQL(
                        "CREATE TRIGGER original BEFORE INSERT "
                        "ON clinic_app.ai_extractionjob FOR EACH ROW "
                        "EXECUTE FUNCTION clinic_app.retained_probe();"
                    )
                ]
            )
        ]
    )
    candidate = SimpleNamespace(
        operations=[
            migrations.RunSQL(
                "CREATE OR REPLACE FUNCTION clinic_app.retained_probe() "
                "RETURNS trigger LANGUAGE plpgsql AS $f$ "
                "BEGIN RETURN NEW; END $f$;"
            )
        ]
    )
    monkeypatch.setattr(
        upgrade,
        "MigrationLoader",
        lambda *_args, **_kwargs: SimpleNamespace(
            disk_migrations={
                ("ai", "0004_historical"): historical,
                ("ai", "0016_candidate"): candidate,
            }
        ),
    )
    assert "ai_extractionjob" in upgrade.altered_ai_tables()


def test_new_affected_model_requires_new_populated_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = SimpleNamespace(
        operations=[
            migrations.AddField(
                model_name="actionreceipt",
                name="upgrade_probe",
                field=models.CharField(max_length=20, default=""),
            )
        ]
    )
    monkeypatch.setattr(
        upgrade,
        "MigrationLoader",
        lambda *_args, **_kwargs: SimpleNamespace(
            disk_migrations={("ai", "0018_probe"): future}
        ),
    )
    assert "ai_actionreceipt" in upgrade.altered_ai_tables()
    assert upgrade.coverage_failures({}) == [
        "AI upgrade seed missing populated table: " + table
        for table in sorted(upgrade.altered_ai_tables())
    ]


def test_present_historical_ai_schema_requires_seed_markers() -> None:
    assert upgrade.verify_ai({"ai_present": False}, required=True) == [
        "AI historical schema was present but seed markers are absent"
    ]


def test_pre_ai_foundation_is_not_claimed_as_populated_ai_history() -> None:
    assert upgrade.verify_ai({"ai_present": False}, required=False) == []


@pytest.mark.parametrize(
    ("histories", "seeds"),
    [
        ({}, ["core"]),
        ({"ai": {"tables": {}}}, ["core", "ai"]),
        ({"inbox": {"cases": {}}, "ai": {"tables": {}}}, ["core", "inbox", "ai"]),
    ],
)
def test_verdict_records_completed_histories(
    histories: dict[str, object], seeds: list[str]
) -> None:
    assert completed_seeds(histories) == seeds
    assert completed_seeds({**histories, "seeds": seeds}) == seeds


def test_verdict_rejects_declared_history_that_did_not_run() -> None:
    with pytest.raises(ValueError, match="disagree with completed histories"):
        completed_seeds({"seeds": ["core", "ai"]})


def test_verdict_cli_preserves_verified_seed_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_path = tmp_path / "seed.json"
    seed_path.write_text('{"ai":{"tables":{"ai_aiinvocation":{"count":14}}}}')
    verified = tmp_path / "verified.json"
    verified.write_text(
        '{"schema_version":1,"result":"PASS","base_sha":"base",'
        '"upgrade_db":"upgrade_rc_123","seeds":["core","ai"],'
        '"database_recreated":false}'
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["verdict", str(seed_path), "base", "upgrade_rc_123", str(verified), "0"],
    )
    publisher.main()
    assert json.loads((tmp_path / "verdict.json").read_text()) == json.loads(
        verified.read_text()
    )
