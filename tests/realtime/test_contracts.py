"""Machine-consumed integration contracts for this table-free transport."""

import ast
import json
from pathlib import Path

import yaml
from apps.core.telemetry import LOG_MESSAGE_ALLOWLIST
from django.apps import apps

ROOT = Path(__file__).resolve().parents[2]


def test_realtime_census_classifications_are_explicit() -> None:
    inventory = json.loads((ROOT / "tests/identity/legacy_guards.json").read_text())
    classifications = {
        row["symbol"]: row["kind"]
        for row in inventory["candidates"]
        if row["symbol"].startswith("apps.realtime.")
    }
    # Plan item 6 certification model: every realtime row is observed or delegated
    # to an executable staff oracle; no reason-only (v2/nonstaff) exemption.
    assert classifications == {
        "apps.realtime.authorization._patient_topics": "direct",
        # Surfaced by the derived permission_helper signal (require_permission).
        "apps.realtime.authorization._staff_topics": "delegated",
        "apps.realtime.authorization._verified_staff": "delegated",
        "apps.realtime.authorization.authorize_topics_sync": "delegated",
        "apps.realtime.hooks.permission_changed": "direct",
        "apps.realtime.scopes._schedule_scope": "infrastructure",
        "apps.realtime.scopes.authorize_scope": "delegated",
        "apps.realtime.scopes.grant_clinic_topic": "delegated",
        "apps.realtime.scopes.register_job_topic": "delegated",
        "apps.realtime.scopes.revoke_clinic_topic": "delegated",
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
    script = next(command for command in commands if "for entry in" in command)
    install = (
        "uv run --frozen --no-sync --no-env-file python -m playwright install ffmpeg"
    )
    assert install in script.splitlines()
    assert script.index(install) < script.index("for entry in")


def test_realtime_log_templates_are_registered_telemetry_messages() -> None:
    # Unregistered templates render as "[unlisted]", hiding the polling signal.
    templates = {
        node.args[0].value
        for path in (ROOT / "apps/realtime").glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert templates
    assert templates <= LOG_MESSAGE_ALLOWLIST, templates - LOG_MESSAGE_ALLOWLIST
