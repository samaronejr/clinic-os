from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Final, Literal

import pytest
from ops.testing import isolation_final_wave_record as final_record
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

from isolation.isolation_final_wave_engine_fixtures import (
    final_record_matrix,
    schema_result,
)

if TYPE_CHECKING:
    from pathlib import Path

type PrimitivePath = tuple[str] | tuple[str, str] | tuple[str, str, Literal["remove"]]
CHILD_TERMINAL_FIELDS: Final = (
    "child_pid",
    "child_pgid",
    "child_start_ticks",
    "child_barrier_released",
    "termination_kind",
)
SUCCESS_MUTATIONS: Final[tuple[tuple[str, JsonObject], ...]] = (
    ("nonzero-exit", {"wait_exit_code": 1}),
    ("missing-start", {"started_at_utc": None}),
    ("missing-pid", {"child_pid": None}),
    ("missing-pgid", {"child_pgid": None}),
    ("missing-start-ticks", {"child_start_ticks": None}),
    ("missing-barrier", {"child_barrier_released": None}),
    ("unreleased-barrier", {"child_barrier_released": False}),
    ("missing-termination", {"termination_kind": None}),
    ("missing-exit", {"wait_exit_code": None}),
    ("signal-outcome", {"wait_exit_code": None, "wait_signal": 9}),
    ("timed-out", {"timed_out": True}),
    ("missing-outcome", {"typed_outcome_sha256": None}),
    (
        "empty-child-outcome",
        {
            "started_at_utc": None,
            "child_pid": None,
            "child_pgid": None,
            "child_start_ticks": None,
            "child_barrier_released": None,
            "termination_kind": None,
            "wait_exit_code": None,
            "wait_signal": None,
            "typed_outcome_sha256": None,
        },
    ),
)
PRIMITIVE_MUTATIONS: Final[tuple[tuple[str, PrimitivePath, JsonValue], ...]] = (
    ("prepared", ("schema_version",), True),
    ("prepared", ("recovery_boot_id",), "not-a-uuid"),
    ("prepared", ("controller_lease_path",), "final-wave-controller.lease"),
    ("prepared", ("controller_lease_state",), "invalid"),
    ("prepared", ("controller_lease_identity", "device"), -1),
    ("prepared", ("controller_lease_identity", "inode"), 0),
    ("prepared", ("controller_lease_identity", "mode"), 420),
    ("prepared", ("controller_lease_identity", "uid"), -1),
    ("prepared", ("controller_lease_identity", "gid"), -1),
    ("prepared", ("controller_lease_identity", "link_count"), 2),
    ("prepared", ("controller_lease_identity", "unexpected"), 1),
    ("prepared", ("controller_lease_identity", "device", "remove"), None),
    ("prepared", ("controller_owners", "sequence"), True),
    ("prepared", ("controller_owners", "acquired_at_utc"), "not-a-timestamp"),
    ("success", ("controller_owners", "relinquished_at_utc"), "not-a-timestamp"),
    ("prepared", ("boot_disappearance_sha256",), "not-a-sha"),
    ("prepared", ("wait_exit_code",), 0),
    ("prepared", ("wait_signal",), 9),
    ("prepared", ("timed_out",), True),
    ("prepared", ("typed_outcome_sha256",), "c" * 64),
    ("prepared", ("cleanup_verified",), "not-a-boolean"),
    ("prepared", ("updated_at_utc",), "not-a-timestamp"),
    ("child-terminal", ("wait_exit_code",), -1),
    ("child-terminal", ("wait_exit_code",), 256),
    ("child-terminal", ("wait_exit_code",), True),
    ("child-terminal", ("wait_signal",), 0),
    ("child-terminal", ("wait_signal",), -1),
    ("child-terminal", ("wait_signal",), True),
    ("child-terminal", ("wait_signal",), "not-an-integer"),
    ("child-terminal", ("timed_out",), "not-a-boolean"),
    ("child-terminal", ("timed_out",), 1),
    ("failure-ready", ("termination_kind",), "invalid"),
)
CONTAINER_VALUE_PATHS: Final[tuple[PrimitivePath, ...]] = (
    ("form",),
    ("state",),
    ("stage",),
    ("final_gate_staging_state",),
    ("controller_lease_state",),
    ("child_barrier_released",),
    ("termination_kind",),
    ("controller_owners", "relinquish_kind"),
)
CONTAINER_VALUES: Final[tuple[JsonValue, ...]] = ([], {})


@pytest.mark.parametrize("field", CHILD_TERMINAL_FIELDS)
def test_child_terminal_runtime_and_schema_require_closed_identity(
    tmp_path: Path, field: str
) -> None:
    # Given: one legal child-terminal record and one missing closed-state field.
    valid = final_record_matrix(tmp_path)["child-terminal"]
    final_record.validate_final_wave_record(valid)
    assert schema_result(tmp_path, 0, valid).returncode == 0
    invalid = copy.deepcopy(valid)
    invalid[field] = None

    # When: both contract validators inspect the same malformed record.
    runtime_rejected = _runtime_rejected(invalid)
    schema_rejected = schema_result(tmp_path, 1, invalid).returncode != 0

    # Then: nullable schema declarations cannot weaken the runtime contract.
    assert (runtime_rejected, schema_rejected) == (True, True)


@pytest.mark.parametrize(
    ("_case", "changes"),
    SUCCESS_MUTATIONS,
    ids=[case for case, _changes in SUCCESS_MUTATIONS],
)
def test_success_runtime_and_schema_require_successful_child_evidence(
    tmp_path: Path, _case: str, changes: JsonObject
) -> None:
    # Given: a legal success control and one misleading success mutation.
    valid = final_record_matrix(tmp_path)["success"]
    final_record.validate_final_wave_record(valid)
    assert schema_result(tmp_path, 2, valid).returncode == 0
    invalid: JsonObject = {**valid, **changes}

    # When: runtime and Draft 2020-12 inspect the claimed success.
    runtime_rejected = _runtime_rejected(invalid)
    schema_rejected = schema_result(tmp_path, 3, invalid).returncode != 0

    # Then: only complete, successful child evidence may close as success.
    assert (runtime_rejected, schema_rejected) == (True, True)


@pytest.mark.parametrize(
    ("state", "path", "value"),
    PRIMITIVE_MUTATIONS,
    ids=[
        f"{state}-{'-'.join(path)}-{index}"
        for index, (state, path, _value) in enumerate(PRIMITIVE_MUTATIONS)
    ],
)
def test_final_wave_runtime_matches_schema_primitive_constraints(
    tmp_path: Path,
    state: str,
    path: PrimitivePath,
    value: JsonValue,
) -> None:
    # Given: one schema-valid public record and one primitive constraint violation.
    invalid = copy.deepcopy(final_record_matrix(tmp_path)[state])
    if len(path) == 1:
        invalid[path[0]] = value
    else:
        field = path[1]
        if path[0] == "controller_lease_identity":
            target = invalid["controller_lease_identity"]
            assert isinstance(target, dict)
        else:
            assert path[0] == "controller_owners"
            owners = invalid["controller_owners"]
            assert isinstance(owners, list)
            target = owners[-1]
            assert isinstance(target, dict)
        if len(path) == 3:
            assert path[2] == "remove"
            _ = target.pop(field)
        else:
            target[field] = value
    if path == ("wait_signal",):
        invalid["wait_exit_code"] = None

    # When: runtime and Draft 2020-12 inspect the same malformed record.
    runtime_rejected = _runtime_rejected(invalid)
    schema_rejected = schema_result(tmp_path, 4, invalid).returncode != 0

    # Then: runtime closes every primitive constraint enforced by the schema.
    assert (runtime_rejected, schema_rejected) == (True, True)


@pytest.mark.parametrize(
    "path",
    CONTAINER_VALUE_PATHS,
    ids=["-".join(path) for path in CONTAINER_VALUE_PATHS],
)
@pytest.mark.parametrize("value", CONTAINER_VALUES, ids=("array", "object"))
def test_final_wave_container_enum_values_raise_typed_boundary_error(
    tmp_path: Path,
    path: PrimitivePath,
    value: JsonValue,
) -> None:
    # Given: a schema-valid prepared record with one enum-like value made a container.
    invalid = copy.deepcopy(final_record_matrix(tmp_path)["prepared"])
    if len(path) == 1:
        invalid[path[0]] = value
    else:
        owners = invalid["controller_owners"]
        assert isinstance(owners, list)
        owner = owners[-1]
        assert isinstance(owner, dict)
        owner[path[1]] = value

    # When: Draft 2020-12 and the public runtime boundary inspect the malformed record.
    schema_rejected = schema_result(tmp_path, 5, invalid).returncode != 0

    # Then: both reject it, and runtime preserves its typed public failure contract.
    assert schema_rejected
    with pytest.raises(IsolationError):
        final_record.validate_final_wave_record(invalid)


def _runtime_rejected(record: JsonObject) -> bool:
    try:
        final_record.validate_final_wave_record(record)
    except IsolationError:
        return True
    return False
