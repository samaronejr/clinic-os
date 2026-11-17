"""Historical AI coverage cannot silently regress to an empty-table upgrade."""

from types import SimpleNamespace

import pytest
from django.db import migrations, models
from ops.testing import migration_upgrade_ai_verify as upgrade


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
    assert upgrade.verify_ai({"ai_present": True}) == [
        "AI historical schema was present but seed markers are absent"
    ]


def test_pre_ai_foundation_is_not_claimed_as_populated_ai_history() -> None:
    assert upgrade.verify_ai({"ai_present": False}) == []
