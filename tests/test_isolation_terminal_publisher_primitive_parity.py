from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable

import pytest
from ops.testing import isolation_terminal_publisher_contract as publisher
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

type PublisherState = Literal[
    "unbound",
    "id-bound",
    "reserved",
    "active",
    "release-intent",
    "released",
]
type PublisherPhase = Literal[
    "initializing",
    "inputs-frozen",
    "lanes-complete",
    "pre-f4-frozen",
    "f4-rejected",
    "f4-created",
    "final-frozen",
    "rejected",
]
type Control = tuple[PublisherState, PublisherPhase]
type ScalarPath = tuple[str, ...]


class SchemaValidator(Protocol):
    def is_valid(self, instance: JsonValue) -> bool: ...


class ValidatorFactory(Protocol):
    def __call__(self, schema: JsonObject) -> SchemaValidator: ...

    def check_schema(self, schema: JsonObject) -> None: ...


@runtime_checkable
class JsonSchemaModule(Protocol):
    Draft202012Validator: ValidatorFactory


@runtime_checkable
class JsonModule(Protocol):
    def loads(self, source: str) -> JsonValue: ...


PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMA_PATH: Final = PROJECT_ROOT / "ops/testing/final-wave-state.schema.json"
JSON_MODULE = importlib.import_module("json")
assert isinstance(JSON_MODULE, JsonModule)
SCHEMA_VALUE = JSON_MODULE.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
assert isinstance(SCHEMA_VALUE, dict)
SCHEMA: Final[JsonObject] = SCHEMA_VALUE
JSON_SCHEMA = importlib.import_module("jsonschema")
assert isinstance(JSON_SCHEMA, JsonSchemaModule)
JSON_SCHEMA.Draft202012Validator.check_schema(SCHEMA)
VALIDATOR: Final = JSON_SCHEMA.Draft202012Validator(SCHEMA)
CONTAINER_VALUES: Final[tuple[JsonValue, ...]] = ([], {})
LEGAL_CONTROLS: Final[tuple[Control, ...]] = (
    ("unbound", "initializing"),
    ("id-bound", "initializing"),
    ("reserved", "initializing"),
    ("active", "initializing"),
    ("active", "inputs-frozen"),
    ("active", "lanes-complete"),
    ("active", "pre-f4-frozen"),
    ("active", "f4-rejected"),
    ("active", "f4-created"),
    ("active", "final-frozen"),
    ("active", "rejected"),
    ("release-intent", "initializing"),
    ("release-intent", "f4-rejected"),
    ("release-intent", "final-frozen"),
    ("release-intent", "rejected"),
    ("released", "initializing"),
    ("released", "f4-rejected"),
    ("released", "final-frozen"),
    ("released", "rejected"),
)


def _resolve(node: JsonObject, schema: JsonObject) -> JsonObject:
    reference = node.get("$ref")
    if not isinstance(reference, str):
        return node
    target: JsonValue = schema
    for component in reference.removeprefix("#/").split("/"):
        assert isinstance(target, dict)
        target = target[component]
    assert isinstance(target, dict)
    return target


def _scalar_paths(
    schema: JsonObject,
    node: JsonObject | None = None,
    prefix: ScalarPath = (),
) -> tuple[ScalarPath, ...]:
    resolved = _resolve(schema if node is None else node, schema)
    properties = resolved.get("properties")
    if isinstance(properties, dict):
        return tuple(
            path
            for name, child in properties.items()
            if isinstance(child, dict)
            for path in _scalar_paths(schema, child, (*prefix, name))
        )
    branches = resolved.get("oneOf")
    if not isinstance(branches, list):
        return (prefix,)
    return tuple(
        dict.fromkeys(
            path
            for branch in branches
            if isinstance(branch, dict)
            and _resolve(branch, schema).get("type") != "null"
            for path in _scalar_paths(schema, branch, prefix)
        )
    )


def test_scalar_paths_discovers_added_nested_identity_leaf() -> None:
    # Given: the normative schema gains one deeper scalar below nullable identity.
    schema = copy.deepcopy(SCHEMA)
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    identity = definitions["identity"]
    assert isinstance(identity, dict)
    properties = identity["properties"]
    assert isinstance(properties, dict)
    properties["mount"] = {
        "type": "object",
        "properties": {"generation": {"type": "integer"}},
    }

    # When: publisher scalar paths are derived from the modified schema.
    paths = _scalar_paths(schema)

    # Then: both nullable identity branches expose the new nested scalar leaf.
    assert ("control_root_identity", "mount", "generation") in paths
    assert ("lock_identity", "mount", "generation") in paths


SCALAR_PATHS: Final = _scalar_paths(SCHEMA)
assert len(SCALAR_PATHS) == 37
MUTATIONS: Final = tuple(
    (control, path, value)
    for control in LEGAL_CONTROLS
    for path in SCALAR_PATHS
    for value in CONTAINER_VALUES
)
assert len(MUTATIONS) == 1_406


@pytest.mark.parametrize(
    "control",
    LEGAL_CONTROLS,
    ids=[f"{state}@{phase}" for state, phase in LEGAL_CONTROLS],
)
def test_runtime_legal_publisher_control_satisfies_exact_schema(
    control: Control,
) -> None:
    # Given: one of the exhaustive runtime-legal publisher phase/state controls.
    journal = _journal(control)

    # When: the public runtime and exact Draft 2020-12 schema inspect the control.
    publisher.validate_final_wave_journal(journal)

    # Then: both contract authorities accept the same complete journal.
    assert VALIDATOR.is_valid(journal)


@pytest.mark.parametrize(
    ("control", "path", "value"),
    MUTATIONS,
    ids=[
        f"{state}@{phase}-{'-'.join(path)}-{type(value).__name__}"
        for (state, phase), path, value in MUTATIONS
    ],
)
def test_publisher_container_scalar_fails_as_typed_isolation_error(
    control: Control,
    path: ScalarPath,
    value: JsonValue,
) -> None:
    # Given: one legal control with exactly one schema scalar made a container.
    invalid = copy.deepcopy(_journal(control))
    target = invalid
    for segment in path[:-1]:
        nested = target[segment]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    # When / Then: both authorities reject it through the public typed boundary.
    assert not VALIDATOR.is_valid(invalid)
    with pytest.raises(IsolationError):
        publisher.validate_final_wave_journal(invalid)


def _journal(control: Control) -> JsonObject:
    state, phase = control
    identity: JsonObject = {"device": 1, "gid": 2, "inode": 3, "mode": 384, "uid": 4}
    journal: JsonObject = {
        "attempt_id": "11111111-1111-4111-8111-111111111111",
        "bootstrap_root": "/clinic-test/final-control-bootstrap-" + "a" * 32,
        "control_root": "/clinic-test/clinic-os-phase1a-final",
        "control_root_identity": copy.deepcopy(identity),
        "f4_sha256": "4" * 64,
        "final_sha256": "5" * 64,
        "inputs_sha256": "1" * 64,
        "lineage_validation_sha256": "6" * 64,
        "lock_identity": copy.deepcopy(identity),
        "phase": phase,
        "pre_f4_sha256": "3" * 64,
        "receipt_lineage_sha256": "7" * 64,
        "schema_version": 1,
        "sha": "8" * 40,
        "terminal_publisher_claim_id": None,
        "terminal_publisher_post_release_ledger_sha256": None,
        "terminal_publisher_post_reservation_ledger_sha256": None,
        "terminal_publisher_pre_release_ledger_sha256": None,
        "terminal_publisher_pre_reservation_ledger_sha256": None,
        "terminal_publisher_release_authorizations_sha256": None,
        "terminal_publisher_release_basis_sha256": None,
        "terminal_publisher_release_boot_id": None,
        "terminal_publisher_release_context": None,
        "terminal_publisher_release_kind": None,
        "terminal_publisher_released_at_utc": None,
        "terminal_publisher_reservation_at_utc": None,
        "terminal_publisher_reservation_spec_sha256": None,
        "terminal_publisher_state": state,
        "updated_at_utc": "2026-07-28T12:00:00.000001Z",
    }
    if state != "unbound":
        _bind_reservation(journal)
    if state in {"release-intent", "released"}:
        _bind_release(journal, released=state == "released")
    return journal


def _bind_reservation(journal: JsonObject) -> None:
    journal["terminal_publisher_claim_id"] = "22222222-2222-4222-8222-222222222222"
    journal["terminal_publisher_post_reservation_ledger_sha256"] = "9" * 64
    journal["terminal_publisher_pre_reservation_ledger_sha256"] = "a" * 64
    journal["terminal_publisher_reservation_at_utc"] = "2026-07-28T12:00:01.000001Z"
    journal["terminal_publisher_reservation_spec_sha256"] = "b" * 64


def _bind_release(journal: JsonObject, *, released: bool) -> None:
    journal["terminal_publisher_post_release_ledger_sha256"] = "c" * 64
    journal["terminal_publisher_pre_release_ledger_sha256"] = "d" * 64
    journal["terminal_publisher_release_authorizations_sha256"] = "e" * 64
    journal["terminal_publisher_release_basis_sha256"] = "f" * 64
    journal["terminal_publisher_release_boot_id"] = (
        "33333333-3333-4333-8333-333333333333"
    )
    journal["terminal_publisher_release_context"] = "same-boot"
    journal["terminal_publisher_release_kind"] = "approved-chain"
    if released:
        journal["terminal_publisher_released_at_utc"] = "2026-07-28T12:00:02.000001Z"
