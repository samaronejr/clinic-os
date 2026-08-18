from __future__ import annotations

from pathlib import Path

from ops.testing.isolation_final_input_auth import REQUIRED_SUITES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
F2_GATE = PROJECT_ROOT / "ops/testing/f2_gate.sh"
EXPECTED = (
    "timeout --signal=TERM --kill-after=120s 1800s make ci",
    'timeout 60s git diff --check cffbb1900ae2132560f20c27fcf1a514a1ef71aa.."$SHA"',
    "timeout 300s uv run ruff check .",
    "timeout 300s uv run ruff format --check .",
    "timeout 600s uv run mypy .",
    "timeout 600s uv run pip-audit --local",
    "timeout 600s uv run pytest -q tests/test_production_settings.py "
    "tests/test_release_settings.py tests/test_browser_settings.py "
    "tests/test_database_options.py tests/test_data_mode.py "
    "tests/test_product_telemetry.py tests/test_timezone_source.py",
    "timeout 600s uv run pytest -q tests/test_schema_policy.py "
    "tests/test_resolver_catalog.py tests/test_readiness.py "
    "tests/test_container_contract.py",
    "timeout 900s uv run pytest -q tests/test_patient_services.py "
    "tests/test_availability_concurrency.py tests/test_appointment_concurrency.py "
    "tests/test_appointment_transition_concurrency.py "
    "tests/test_lifecycle_lock_races.py",
    "timeout 900s uv run pytest -q tests/test_phase1_audit_contract.py "
    "tests/test_phase1_audit_migration.py tests/test_audit_append.py "
    "tests/test_audit_unicode_boundaries.py",
    "timeout 300s uv run python ops/testing/assert_foundation_history.py "
    "cffbb1900ae2132560f20c27fcf1a514a1ef71aa",
    "timeout --signal=TERM --kill-after=120s 900s "
    'ops/testing/image_contract_gate.sh --sha "$SHA" --inputs '
    ".omo/evidence/clinic-os-phase1a-final/terminal/inputs.json",
    "timeout --signal=TERM --kill-after=120s 600s ops/testing/tls_stack.sh smoke",
    "timeout --signal=TERM --kill-after=120s 600s uv run pytest -q "
    "tests/test_isolated_db_harness.py tests/test_isolation_ledger.py",
)


def test_f2_gate_contains_the_closed_ordered_stage_list() -> None:
    # Given: the committed F2 prerequisite runner.
    assert F2_GATE.is_file()
    source = F2_GATE.read_text(encoding="utf-8")

    # When / Then: each exact stage appears once and strictly in plan order.
    offsets = [source.index(command) for command in EXPECTED]
    assert offsets == sorted(offsets)
    assert all(source.count(command) == 1 for command in EXPECTED)
    assert "STAGE_CLEANUP_SECONDS=120" in source
    assert "SUPERVISOR_CLEANUP_SECONDS=150" in source
    assert "OUTER_FORCE_KILL_SECONDS=180" in source
    assert "mktemp" not in source
    assert 'work="$evidence_parent/.${evidence_name}.records"' in source
    assert '[[ -d "$evidence_parent" && ! -L "$evidence_parent"' in source


def test_image_contract_gate_never_rebuilds_or_retags() -> None:
    # Given: the stage-12 immutable image contract gate.
    script = PROJECT_ROOT / "ops/testing/image_contract_gate.sh"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")

    # When / Then: it only validates existing content-addressed candidates.
    assert "validate_image_contract" in source
    for forbidden in (
        "docker build",
        "docker tag",
        "docker pull",
        "image_smoke.sh build",
    ):
        assert forbidden not in source


def test_ci_three_suite_contract_stays_distinct_from_final_four_suites() -> None:
    ci_contract = PROJECT_ROOT / "ops/testing/ci-required-browser-suites.txt"
    final_contract = PROJECT_ROOT / "ops/testing/final-required-browser-suites.txt"

    assert ci_contract.read_bytes() == b"availability\npatient\nscheduling\n"
    assert REQUIRED_SUITES == (
        "availability",
        "patient",
        "runtime-https",
        "scheduling",
    )
    assert not final_contract.exists()
