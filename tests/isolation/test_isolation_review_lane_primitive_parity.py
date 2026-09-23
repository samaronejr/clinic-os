from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import pytest
from ops.testing import isolation_review_lane_record as review_record
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[2]
    / "ops/testing/review-lane-controller-state.schema.json"
)
UUID: Final = "11111111-1111-4111-8111-111111111111"
BOOT: Final = "22222222-2222-4222-8222-222222222222"
CLAIM: Final = "44444444-4444-4444-8444-444444444444"
PRIVATE_CLAIM: Final = "55555555-5555-4555-8555-555555555555"
DIGEST: Final = "c" * 64
NOW: Final = "2026-07-16T12:00:00.000000Z"
type PrimitivePath = tuple[str, ...]


@runtime_checkable
class _JsonModule(Protocol):
    def loads(self, source: str) -> JsonValue: ...


class _SchemaValidator(Protocol):
    def is_valid(self, instance: JsonValue) -> bool: ...


class _ValidatorFactory(Protocol):
    def __call__(self, schema: JsonObject) -> _SchemaValidator: ...


@runtime_checkable
class _JsonSchemaModule(Protocol):
    Draft202012Validator: _ValidatorFactory


def _load_schema() -> JsonObject:
    json_module = importlib.import_module("json")
    assert isinstance(json_module, _JsonModule)
    value = json_module.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _resolved(node: JsonObject, schema: JsonObject) -> JsonObject:
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
    node: JsonObject, schema: JsonObject, prefix: PrimitivePath = ()
) -> tuple[PrimitivePath, ...]:
    resolved = _resolved(node, schema)
    properties = resolved.get("properties")
    if isinstance(properties, dict):
        paths: tuple[PrimitivePath, ...] = ()
        for name, child in properties.items():
            assert isinstance(child, dict)
            paths += _scalar_paths(child, schema, (*prefix, name))
        return paths
    items = resolved.get("items")
    if isinstance(items, dict):
        return _scalar_paths(items, schema, prefix)
    return (prefix,)


def _completed(stage: str, offset: int = 0) -> JsonObject:
    return {
        "stage": stage,
        "process_claim_id": f"55555555-5555-4555-8555-{551 + offset:012d}",
        "child_pid": 6001 + offset,
        "child_pgid": 6001 + offset,
        "child_start_ticks": 10001 + offset,
        "barrier_released": True,
        "argv_sha256": DIGEST,
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "typed_outcome_sha256": DIGEST,
    }


def _base_control() -> JsonObject:
    return {
        "schema_version": 1,
        "attempt_id": UUID,
        "lane": "F2",
        "sha": "a" * 40,
        "inputs_sha256": DIGEST,
        "creation_boot_id": BOOT,
        "recovery_boot_id": None,
        "state": "between-stages",
        "stage": "prerequisites",
        "controller_lease_path": "/clinic-os/review-lane-controllers/F2.lease",
        "controller_lease_identity": {
            "device": 1,
            "inode": 2,
            "mode": 384,
            "uid": 1000,
            "gid": 1000,
            "link_count": 1,
        },
        "controller_lease_state": "held",
        "controller_owners": [
            {
                "sequence": 1,
                "boot_id": BOOT,
                "pid": 6000,
                "start_ticks": 10000,
                "acquired_at_utc": NOW,
                "relinquished_at_utc": None,
                "relinquish_kind": None,
            }
        ],
        "completed_stages": [_completed("environment-sync")],
        "review_workspace_claim_id": CLAIM,
        "private_environment_claim_id": PRIVATE_CLAIM,
        "filesystem_state": "active",
        "codex_tool_sha256": DIGEST,
        "uv_tool_sha256": DIGEST,
        "private_tree_sha256": DIGEST,
        "child_process_claim_id": None,
        "child_argv_sha256": None,
        "started_at_utc": None,
        "child_pid": None,
        "child_pgid": None,
        "child_start_ticks": None,
        "child_barrier_released": None,
        "termination_kind": None,
        "boot_disappearance_sha256": None,
        "wait_exit_code": None,
        "wait_signal": None,
        "timed_out": False,
        "typed_outcome_sha256": None,
        "child_cleanup_verified": True,
        "filesystem_cleanup_verified": False,
        "terminal_outputs_sha256": None,
        "updated_at_utc": NOW,
    }


def _control(state: str) -> JsonObject:
    record = _base_control()
    record["state"] = state
    if state == "prepared":
        record.update(
            {
                "filesystem_state": "unreserved",
                "codex_tool_sha256": None,
                "uv_tool_sha256": None,
                "private_tree_sha256": None,
                "child_cleanup_verified": False,
            }
        )
    elif state == "filesystem-reserved":
        record["filesystem_state"] = "reserved"
    elif state in {"child-barrier", "child-running", "child-terminal"}:
        record.update(
            {
                "child_process_claim_id": CLAIM,
                "child_argv_sha256": DIGEST,
                "started_at_utc": NOW,
                "child_pid": 7000,
                "child_pgid": 7000,
                "child_start_ticks": 11000,
                "child_barrier_released": state != "child-barrier",
                "termination_kind": (
                    "wait-result" if state == "child-terminal" else None
                ),
                "wait_exit_code": 0 if state == "child-terminal" else None,
                "typed_outcome_sha256": (DIGEST if state == "child-terminal" else None),
                "child_cleanup_verified": False,
            }
        )
    elif state in {"failure-ready", "success"}:
        owners = record["controller_owners"]
        assert isinstance(owners, list)
        owner = owners[0]
        assert isinstance(owner, dict)
        owner.update({"relinquished_at_utc": NOW, "relinquish_kind": "clean-release"})
        record.update(
            {
                "controller_lease_state": "released",
                "filesystem_state": "released",
                "filesystem_cleanup_verified": True,
            }
        )
        if state == "success":
            record.update(
                {
                    "stage": "review",
                    "completed_stages": [
                        _completed("environment-sync"),
                        _completed("prerequisites", 1),
                        _completed("review", 2),
                    ],
                    "terminal_outputs_sha256": DIGEST,
                }
            )
    return record


def _mutate(record: JsonObject, path: PrimitivePath, value: JsonValue) -> None:
    target = record
    for component in path[:-1]:
        child = target[component]
        if isinstance(child, list):
            child = child[0]
        assert isinstance(child, dict)
        target = child
    target[path[-1]] = value


SCHEMA: Final = _load_schema()
JSON_SCHEMA = importlib.import_module("jsonschema")
assert isinstance(JSON_SCHEMA, _JsonSchemaModule)
VALIDATOR: Final = JSON_SCHEMA.Draft202012Validator(SCHEMA)
PATHS: Final = _scalar_paths(SCHEMA, SCHEMA)
STATES: Final = (
    "prepared",
    "filesystem-reserved",
    "filesystem-active",
    "child-barrier",
    "child-running",
    "child-terminal",
    "between-stages",
    "recovering",
    "failure-ready",
    "success",
)
CONTROLS: Final = tuple((state, _control(state)) for state in STATES)
MALFORMED_CONTAINERS: Final[tuple[JsonValue, JsonValue]] = ([], {})
MUTATIONS: Final[tuple[tuple[str, PrimitivePath, JsonValue], ...]] = tuple(
    (state, path, shape)
    for state in STATES
    for path in PATHS
    for shape in MALFORMED_CONTAINERS
)
assert len(PATHS) == 58
assert len(MUTATIONS) == 1160


@pytest.mark.parametrize(("state", "record"), CONTROLS, ids=STATES)
def test_review_lane_schema_derived_control_is_dual_valid(
    state: str, record: JsonObject
) -> None:
    # Given: one closed legal control for a recognized review-lane state.
    assert record["state"] == state

    # When: Draft 2020-12 and the public runtime inspect the schema-derived record.
    assert VALIDATOR.is_valid(record)
    review_record.validate_review_lane_record(record)

    # Then: the unmodified control is accepted without compatibility coercion.


@pytest.mark.parametrize(
    ("state", "path", "shape"),
    MUTATIONS,
    ids=(
        f"{state}-{'-'.join(path)}-{'array' if isinstance(shape, list) else 'object'}"
        for state, path, shape in MUTATIONS
    ),
)
def test_review_lane_schema_scalar_container_raises_typed_boundary_error(
    state: str, path: PrimitivePath, shape: JsonValue
) -> None:
    # Given: one dual-valid control with exactly one schema scalar made a container.
    invalid = copy.deepcopy(_control(state))
    _mutate(invalid, path, shape)

    # When / Then: both authorities reject the same mutation.
    assert not VALIDATOR.is_valid(invalid)
    with pytest.raises(IsolationError):
        review_record.validate_review_lane_record(invalid)
