from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final, Protocol, runtime_checkable

import pytest
from ops.testing import isolation_final_wave_record as final_record
from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_accepted_close_records import RECEIPT_KEYS, STATE_KEYS
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
)
from ops.testing.isolation_terminal_artifact import ARTIFACT_KEYS
from ops.testing.isolation_terminal_publisher_journal import (
    JOURNAL_KEYS,
    validate_initial_publisher_journal,
)

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
OPEN_FINAL_WAVE_ROOT_FRAGMENTS: Final = frozenset(
    {
        "boundReservation",
        "releaseIntent",
        "releasedPublisher",
        "unboundPublisher",
        "unreleasedPublisher",
    }
)


@runtime_checkable
class _JsonModule(Protocol):
    def loads(self, source: str) -> JsonValue: ...


type _Run = CompletedProcess[str]


@runtime_checkable
class _FinalWaveFixtures(Protocol):
    def final_record_matrix(self, root: Path) -> dict[str, JsonObject]: ...
    def schema_result(self, root: Path, index: int, record: JsonObject) -> _Run: ...


@runtime_checkable
class _RejectionFixtures(Protocol):
    def empty_inventory(self) -> JsonObject: ...


@runtime_checkable
class _PublisherFixtures(Protocol):
    def stale_publisher_ledger(self, root: Path, state: str) -> tuple[Path, Path]: ...


class _UserGateFixture(Protocol):
    ledger_path: Path


@runtime_checkable
class _UserFixtures(Protocol):
    def user_gate_fixture(self, root: Path) -> _UserGateFixture: ...


FINAL_FIXTURES: Final = importlib.import_module("isolation_final_wave_engine_fixtures")
assert isinstance(FINAL_FIXTURES, _FinalWaveFixtures)
REJECTION_FIXTURES: Final = importlib.import_module("isolation_rejection_fixtures")
assert isinstance(REJECTION_FIXTURES, _RejectionFixtures)
PUBLISHER: Final = importlib.import_module("isolation_terminal_publisher_fixtures")
assert isinstance(PUBLISHER, _PublisherFixtures)
USER_FIXTURES: Final = importlib.import_module("isolation_user_fixtures")
assert isinstance(USER_FIXTURES, _UserFixtures)


def _load_object(path: Path) -> JsonObject:
    json_module = importlib.import_module("json")
    assert isinstance(json_module, _JsonModule)
    value = json_module.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def test_terminal_and_accepted_schemas_close_their_exact_roots() -> None:
    # Given: the four immutable terminal, journal, close, and receipt contracts.
    contracts = {
        "accepted-close-state.schema.json": STATE_KEYS,
        "accepted-final-receipt.schema.json": RECEIPT_KEYS,
        "final-wave-state.schema.json": JOURNAL_KEYS,
        "terminal-revalidation.schema.json": ARTIFACT_KEYS,
    }

    # When: each normative schema and every nested object are inspected.
    schemas = {
        name: _load_object(PROJECT_ROOT / "ops" / "testing" / name)
        for name in contracts
    }

    # Then: aliases and unknown fields are closed at every object boundary.
    for name, keys in contracts.items():
        schema = schemas[name]
        properties = _object(schema["properties"])
        required = schema["required"]
        assert isinstance(required, list)
        definitions = _object(schema.get("$defs", {}))
        open_fragments: set[int] = set()
        assert set(properties) == keys
        assert set(required) == keys
        assert schema["additionalProperties"] is False
        if name == "final-wave-state.schema.json":
            identity = _object(definitions["identity"])
            open_fragments = {
                id(definitions[fragment]) for fragment in OPEN_FINAL_WAVE_ROOT_FRAGMENTS
            }
            assert identity["additionalProperties"] is False
            assert all(
                _object(definitions[fragment]).get("additionalProperties") is not False
                for fragment in OPEN_FINAL_WAVE_ROOT_FRAGMENTS
            )
        assert all(
            item.get("additionalProperties") is False or id(item) in open_fragments
            for item in _object_schemas(schema)
        )


def test_final_wave_schema_exposes_the_exact_phase_and_publisher_matrix() -> None:
    # Given: the persistent publisher journal schema.
    path = PROJECT_ROOT / "ops" / "testing" / "final-wave-state.schema.json"

    # When: its phase/state discriminators and conditional matrix are read.
    schema = _load_object(path)
    properties = _object(schema["properties"])
    matrix = json.dumps(schema["allOf"], sort_keys=True)
    phase = _object(properties["phase"])
    publisher_state = _object(properties["terminal_publisher_state"])

    # Then: all legal states are explicit and phase/state coupling is conditional.
    assert phase["enum"] == [
        "initializing",
        "inputs-frozen",
        "lanes-complete",
        "pre-f4-frozen",
        "f4-rejected",
        "f4-created",
        "final-frozen",
        "rejected",
    ]
    assert publisher_state["enum"] == [
        "unbound",
        "id-bound",
        "reserved",
        "active",
        "release-intent",
        "released",
    ]
    for state in ("unbound", "id-bound", "release-intent", "released"):
        assert f'"const": "{state}"' in matrix


def test_accepted_close_state_and_receipt_validate_as_draft_2020_12(
    tmp_path: Path,
) -> None:
    # Given: one completed accepted close and the two persisted contract instances.
    fixture = USER_FIXTURES.user_gate_fixture(tmp_path)
    receipt = fixture.ledger_path.with_name("isolation-ledger-final-phase1a.json")
    _ = close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt,
        inventory_reader=REJECTION_FIXTURES.empty_inventory,
    )
    ledger, _ = load_json(fixture.ledger_path)
    state = Path(str(ledger["attempt_root"])) / "accepted-close-state.json"
    # When: the normative schemas validate their corresponding durable instances.
    returncodes: list[int] = []
    for schema_name, instance in (
        ("accepted-close-state.schema.json", state),
        ("accepted-final-receipt.schema.json", receipt),
    ):
        process_id = os.posix_spawn(
            "/usr/bin/jsonschema",
            (
                "jsonschema",
                "-V",
                "Draft202012Validator",
                str(PROJECT_ROOT / "ops/testing" / schema_name),
                "-i",
                str(instance),
            ),
            os.environ,
        )
        _process_id, status = os.waitpid(process_id, 0)
        returncodes.append(os.waitstatus_to_exitcode(status))

    # Then: both closed records satisfy their exact committed schemas.
    assert returncodes == [0, 0]


def test_public_final_wave_records_validate_as_draft_2020_12(
    tmp_path: Path,
) -> None:
    # Given: every durable final-form state emitted by the public controller.
    records = list(FINAL_FIXTURES.final_record_matrix(tmp_path).values())

    # When: Draft 2020-12 validates the exact emitted records.
    results = [
        FINAL_FIXTURES.schema_result(tmp_path, index, record)
        for index, record in enumerate(records)
    ]

    # Then: runtime-valid public records and the committed schema agree.
    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    invalid = records[-1].copy()
    invalid["staged_outcome_sha256"] = "not-a-sha"
    with pytest.raises(IsolationError, match="staging"):
        final_record.validate_final_wave_record(invalid)


def test_final_wave_runtime_and_schema_reject_the_same_invalid_matrix(
    tmp_path: Path,
) -> None:
    # Given: legal controls for every matrix edge reported by parity review.
    records = FINAL_FIXTURES.final_record_matrix(tmp_path)
    mutations: dict[str, JsonObject] = {
        "prepared": {"final_gate_staging_state": "released"},
        "failure-ready": {"final_gate_staging_state": "active"},
        "between-stages": {"completed_stages": ["f4-decision", "final-freeze"]},
        "child-terminal": {"typed_outcome_sha256": "not-a-sha"},
    }
    failures: list[str] = []

    # When: each legal control and its one-field invalid mutation are validated.
    for index, (state, changes) in enumerate(mutations.items()):
        valid = records[state]
        final_record.validate_final_wave_record(valid)
        if FINAL_FIXTURES.schema_result(tmp_path, index * 2, valid).returncode != 0:
            failures.append(f"schema rejected legal {state}")
        invalid: JsonObject = {**valid, **changes}
        try:
            final_record.validate_final_wave_record(invalid)
        except IsolationError:
            runtime_rejected = True
        else:
            runtime_rejected = False
        schema_rejected = (
            FINAL_FIXTURES.schema_result(tmp_path, index * 2 + 1, invalid).returncode
            != 0
        )
        if not runtime_rejected or not schema_rejected:
            runtime_status = f"runtime_rejected={runtime_rejected}"
            schema_status = f"schema_rejected={schema_rejected}"
            failures.append(f"{state}: {runtime_status}, {schema_status}")

    # Then: both validators reject every illegal edge and accept its control.
    assert failures == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("phase", "inputs-frozen", "phase"),
        (
            "terminal_publisher_reservation_at_utc",
            "not-a-timestamp",
            "timestamp",
        ),
    ],
)
def test_initial_publisher_validator_rejects_schema_invalid_records(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    # Given: a valid reserved publisher and one schema-invalid journal field.
    ledger_path, journal_path = PUBLISHER.stale_publisher_ledger(tmp_path, "reserved")
    ledger, _raw = load_json(ledger_path)
    journal, _raw = load_json(journal_path)
    invalid = copy.deepcopy(journal)
    invalid[field] = value
    claims = ledger["claims"]
    assert isinstance(claims, list)
    claim = claims[0]
    assert isinstance(claim, dict)

    # When / Then: production validation refuses the invalid matrix member.
    with pytest.raises(IsolationError, match=message):
        _ = validate_initial_publisher_journal(ledger, claim, invalid)


def _object_schemas(value: JsonValue) -> list[JsonObject]:
    if isinstance(value, list):
        return [item for entry in value for item in _object_schemas(entry)]
    if not isinstance(value, dict):
        return []
    current = [value] if value.get("type") == "object" else []
    return current + [
        item for entry in value.values() for item in _object_schemas(entry)
    ]
