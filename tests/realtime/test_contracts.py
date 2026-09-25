"""Machine-consumed integration contracts for this table-free transport."""

import json
from pathlib import Path

import yaml
from django.apps import apps

ROOT = Path(__file__).resolve().parents[2]


def test_realtime_census_classifications_are_explicit() -> None:
    inventory = json.loads((ROOT / "tests/identity/legacy_guards.json").read_text())
    classifications = {
        row["symbol"]: row["kind"]
        for row in inventory["candidates"]
        if row["symbol"].startswith("apps.realtime.")
    }
    assert classifications == {
        "apps.realtime.authorization._patient_topics": "nonstaff",
        "apps.realtime.authorization._verified_staff": "v2",
        "apps.realtime.authorization.authorize_topics_sync": "v2",
        "apps.realtime.hooks.permission_changed": "infrastructure",
        "apps.realtime.scopes._schedule_scope": "infrastructure",
        "apps.realtime.scopes.authorize_scope": "v2",
        "apps.realtime.scopes.grant_clinic_topic": "v2",
        "apps.realtime.scopes.revoke_clinic_topic": "v2",
    }


def test_realtime_app_remains_table_free() -> None:
    config = apps.get_app_config("realtime")
    assert config.name == "apps.realtime"
    assert list(config.get_models()) == []


def test_browser_shards_install_video_dependency_before_running_suites() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    commands = [
        step.get("run", "") for step in workflow["jobs"]["renewal-browser"]["steps"]
    ]
    script = next(command for command in commands if "for suite in" in command)
    install = (
        "uv run --frozen --no-sync --no-env-file python -m playwright install ffmpeg"
    )
    assert install in script.splitlines()
    assert script.index(install) < script.index("for suite in")
