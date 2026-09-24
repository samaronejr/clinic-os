from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_ROOT: Final = PROJECT_ROOT / "ops" / "testing"
FIXTURE_ROOT: Final = PROJECT_ROOT / "tests" / "fixtures" / "isolation" / "normative"
SCHEMA_NAMES: Final = (
    "execution-host-proof",
    "cgroup-capability-probe",
    "receipt-lineage-seed",
    "receipt-lineage-validation",
    "receipt-lineage",
    "rejection-spec",
    "rejection-close",
    "archive-rollover-state",
    "archive-rollover-sentinel",
    "failure-receipt",
    "user-fix-intent",
    "user-fix-binding",
    "final-wave-controller-state",
    "review-lane-controller-state",
    "f3-supervisor-state",
)
CLAIM_STATES: Final = ("reserved", "prepared", "active")
CAUSE_PRECEDENCE: Final = (
    "reviewer-reject",
    "product-assertion",
    "auth-rejected",
    "malformed-verdict",
    "malformed-receipt",
    "deadline-exceeded",
    "session-reused",
    "session-disconnected",
    "session-missing",
    "auth-unavailable",
    "launcher-unavailable",
    "dependency-unavailable",
    "io-failure",
    "unexpected-signal",
    "boot-changed",
    "cleanup-failure",
)

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def _run_validator(schema: Path, instance: Path) -> subprocess.CompletedProcess[str]:
    validator = shutil.which("jsonschema")
    assert validator is not None, "the repository JSON Schema validator is required"
    return subprocess.run(  # noqa: S603
        [
            validator,
            "-V",
            "Draft202012Validator",
            str(schema),
            "-i",
            str(instance),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _object_schemas(value: JsonValue) -> list[dict[str, JsonValue]]:
    if isinstance(value, list):
        return [item for entry in value for item in _object_schemas(entry)]
    if not isinstance(value, dict):
        return []
    current = [value] if value.get("type") == "object" else []
    return current + [
        item for entry in value.values() for item in _object_schemas(entry)
    ]


def _observed_causes(path: Path) -> list[str]:
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    causes = value["observed_causes"]
    assert isinstance(causes, list)
    assert all(isinstance(item, str) for item in causes)
    return [item for item in causes if isinstance(item, str)]


@pytest.mark.parametrize("schema_name", SCHEMA_NAMES)
def test_normative_schema_is_draft_2020_and_recursively_closed(
    schema_name: str,
) -> None:
    # Given: a standalone normative contract named by the Phase 1A plan.
    path = SCHEMA_ROOT / f"{schema_name}.schema.json"

    # When: its dialect and every locally declared object are inspected.
    assert path.is_file(), f"missing normative schema: {path.name}"
    schema: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    object_schemas = _object_schemas(schema)

    # Then: Draft 2020-12 and closed object boundaries are mandatory.
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert object_schemas
    assert all(item.get("additionalProperties") is False for item in object_schemas)


@pytest.mark.parametrize("schema_name", SCHEMA_NAMES)
def test_normative_schema_accepts_its_canonical_fixture(schema_name: str) -> None:
    # Given: one canonical synthetic fixture for each standalone schema.
    schema = SCHEMA_ROOT / f"{schema_name}.schema.json"
    fixture = FIXTURE_ROOT / schema_name / "valid.json"

    # When: the repository's Draft 2020-12 validator checks the fixture.
    assert fixture.is_file(), f"missing valid fixture: {fixture}"
    result = _run_validator(schema, fixture)

    # Then: the exact fixture is accepted without weakening the schema.
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("schema_name", SCHEMA_NAMES)
def test_normative_schema_rejects_every_committed_negative_fixture(
    schema_name: str,
) -> None:
    # Given: closed negative fixtures for every standalone schema.
    schema = SCHEMA_ROOT / f"{schema_name}.schema.json"
    fixture_dir = FIXTURE_ROOT / schema_name
    fixtures = sorted(fixture_dir.glob("invalid-*.json"))

    # When: each malformed contract is validated independently.
    assert fixtures, f"missing negative fixture for {schema_name}"
    results = [(fixture, _run_validator(schema, fixture)) for fixture in fixtures]

    # Then: unknown keys, states, nullability, and fixed-order drift fail closed.
    assert all(result.returncode != 0 for _fixture, result in results), [
        str(fixture) for fixture, result in results if result.returncode == 0
    ]


@pytest.mark.parametrize(
    "fixture_name",
    ["valid-disappearance-intent.json", "valid-boot-disappearance.json"],
)
def test_cgroup_schema_accepts_changed_boot_crash_prefixes(fixture_name: str) -> None:
    # Given: a crash-durable changed-boot prefix or its sealed terminal record.
    schema = SCHEMA_ROOT / "cgroup-capability-probe.schema.json"
    fixture = FIXTURE_ROOT / "cgroup-capability-probe" / fixture_name

    # When: the normative probe schema validates the absence-only transition.
    result = _run_validator(schema, fixture)

    # Then: recovery intent and boot-disappearance completion remain portable.
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("state", CLAIM_STATES)
def test_ledger_claim_examples_cover_reserved_prepared_and_active(
    state: str,
    tmp_path: Path,
) -> None:
    # Given: the normative ledger claim matrix and one state-specific example.
    schema = SCHEMA_ROOT / "isolation-ledger.schema.json"
    fixture = FIXTURE_ROOT / "ledger-claims" / f"valid-{state}.json"

    # When: each claim is embedded into the committed valid ledger envelope.
    ledger: JsonValue = json.loads(
        (FIXTURE_ROOT / "ledger-claims" / "ledger-envelope.json").read_text(
            encoding="utf-8"
        )
    )
    claim: JsonValue = json.loads(fixture.read_text(encoding="utf-8"))
    assert isinstance(ledger, dict)
    assert isinstance(claim, dict)
    ledger["claims"] = [claim]
    rendered = tmp_path / f"ledger-{state}.json"
    rendered.write_text(
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = _run_validator(schema, rendered)

    # Then: all three lifecycle examples are validator-backed and canonical.
    assert result.returncode == 0, result.stderr


def test_failure_receipt_examples_cover_exact_cause_precedence() -> None:
    # Given: one canonical multi-cause receipt and one tail inversion.
    fixture_dir = FIXTURE_ROOT / "failure-receipt"
    valid = _observed_causes(fixture_dir / "valid.json")
    reordered = _observed_causes(fixture_dir / "reordered-tail.json")
    rank = {cause: index for index, cause in enumerate(CAUSE_PRECEDENCE)}

    # When: both arrays are compared with the normative precedence table.
    sorted_valid = sorted(valid, key=rank.__getitem__)
    sorted_reordered = sorted(reordered, key=rank.__getitem__)

    # Then: semantic dominance stays ordered beyond the first cause.
    assert valid == sorted_valid
    assert reordered != sorted_reordered
