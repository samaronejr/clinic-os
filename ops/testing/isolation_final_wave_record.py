"""Closed final-wave journal schema and record projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from ops.testing import isolation_controller_kernel as kernel
from ops.testing import isolation_final_wave_primitives as primitives

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject, JsonValue

type FinalForm = Literal["inputs", "scope-pre", "pre-f4", "final"]
RECORD_KEYS: Final[frozenset[str]] = frozenset(
    re.findall(
        r"[a-z0-9_]+",
        """schema_version attempt_id sha form creation_boot_id recovery_boot_id state
        stage completed_stages predecessor_evidence_sha256 pre_f4_sha256
        final_gate_staging_claim_id final_gate_staging_state staged_f4_sha256
        staged_outcome_sha256 controller_lease_path controller_lease_identity
        controller_lease_state controller_owners child_argv_sha256 started_at_utc
        child_pid child_pgid child_start_ticks child_barrier_released
        termination_kind boot_disappearance_sha256 wait_exit_code wait_signal
        timed_out typed_outcome_sha256 cleanup_verified updated_at_utc""",
    )
)
STATES: Final[frozenset[str]] = frozenset(
    re.findall(
        r"[a-z-]+",
        """prepared child-barrier child-running child-terminal between-stages
        recovering failure-ready success""",
    )
)


@dataclass(frozen=True, slots=True)
class PreF4Evidence:
    """Authenticated evidence required by downstream final-wave forms."""

    inputs_sha256: str
    f1_record: JsonObject
    f2_record: JsonObject
    f3_manifest_sha256: str
    scope_pre_sha256: str


@dataclass(frozen=True, slots=True)
class FinalWaveStart:
    """Immutable inputs for acquiring a final-wave controller."""

    attempt_id: str
    sha: str
    form: FinalForm
    boot_id: str
    journal_path: Path
    lease_path: Path
    child_argv_sha256: str
    owner_pid: int
    owner_start_ticks: int
    acquired_at_utc: str
    final_gate_staging_claim_id: str | None = None
    predecessor_evidence: PreF4Evidence | None = None
    pre_f4_sha256: str | None = None


def _validate_final_wave_start(request: FinalWaveStart) -> None:
    if not kernel.is_uuid(request.attempt_id) or not kernel.is_uuid(request.boot_id):
        kernel.fail("final-wave UUID input is invalid")
    source_valid = primitives.is_sha40(request.sha)
    if not source_valid or not kernel.is_sha256(request.child_argv_sha256):
        kernel.fail("final-wave digest input is invalid")
    if kernel.is_uuid(request.final_gate_staging_claim_id) != (request.form == "final"):
        kernel.fail("final-wave staging claim matrix is invalid")
    downstream = request.form in {"pre-f4", "final"}
    if (request.predecessor_evidence is not None) != downstream or (
        kernel.is_sha256(request.pre_f4_sha256) != (request.form == "final")
    ):
        kernel.fail("final-wave predecessor matrix is invalid")
    if not request.journal_path.is_absolute() or not request.lease_path.is_absolute():
        kernel.fail("final-wave controller paths are not absolute")


def _initial_final_wave_record(
    request: FinalWaveStart,
    identity: JsonObject,
    predecessor_evidence_sha256: str | None,
) -> JsonObject:
    final = request.form == "final"
    return {
        "schema_version": 1,
        "attempt_id": request.attempt_id,
        "sha": request.sha,
        "form": request.form,
        "creation_boot_id": request.boot_id,
        "recovery_boot_id": None,
        "state": "prepared",
        "stage": "f4-decision" if final else None,
        "completed_stages": [],
        "predecessor_evidence_sha256": predecessor_evidence_sha256,
        "pre_f4_sha256": request.pre_f4_sha256,
        "final_gate_staging_claim_id": request.final_gate_staging_claim_id,
        "final_gate_staging_state": "unreserved" if final else None,
        "staged_f4_sha256": None,
        "staged_outcome_sha256": None,
        "controller_lease_path": str(request.lease_path),
        "controller_lease_identity": identity,
        "controller_lease_state": "held",
        "controller_owners": [kernel.new_controller_owner(request, 1)],
        "child_argv_sha256": request.child_argv_sha256,
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
        "cleanup_verified": False,
        "updated_at_utc": request.acquired_at_utc,
    }


def _validate_final_wave_record(record: JsonObject) -> None:
    state, form = record.get("state"), record.get("form")
    if frozenset(record) != RECORD_KEYS:
        kernel.fail("final-wave controller record has an open root")
    primitives.validate_final_wave_primitives(record)
    if state not in STATES or form not in {"inputs", "scope-pre", "pre-f4", "final"}:
        kernel.fail("final-wave controller form or state is invalid")
    uuid_fields = ("attempt_id", "creation_boot_id")
    if not all(kernel.is_uuid(record.get(key)) for key in uuid_fields):
        kernel.fail("final-wave controller UUID is invalid")
    terminal = state in {"failure-ready", "success"}
    if (record.get("controller_lease_state") == "released") != terminal:
        kernel.fail("final-wave lease state differs from its controller state")
    owners = kernel.controller_owners(record)
    if (owners[-1].get("relinquished_at_utc") is not None) != terminal:
        kernel.fail("final-wave owner state differs from its controller state")
    if terminal and record.get("cleanup_verified") is not True:
        kernel.fail("final-wave terminal cleanup is unverified")
    _validate_form(record, form)
    _validate_child(record, state)


def _validate_form(record: JsonObject, form: JsonValue) -> None:
    if kernel.is_sha256(record.get("predecessor_evidence_sha256")) != (
        form in {"pre-f4", "final"}
    ) or kernel.is_sha256(record.get("pre_f4_sha256")) != (form == "final"):
        kernel.fail("final-wave predecessor evidence matrix is invalid")
    final_fields = (
        "stage",
        "final_gate_staging_claim_id",
        "final_gate_staging_state",
        "staged_f4_sha256",
        "staged_outcome_sha256",
    )
    if form != "final":
        if (
            any(record.get(key) is not None for key in final_fields)
            or record.get("completed_stages") != []
        ):
            kernel.fail("nonfinal controller carries final staging state")
        return
    if not kernel.is_uuid(record.get("final_gate_staging_claim_id")):
        kernel.fail("final controller staging claim is invalid")
    stage, completed = record.get("stage"), record.get("completed_stages")
    if stage == "f4-decision":
        staging = record.get("final_gate_staging_state")
        terminal_failure = record.get("state") == "failure-ready"
        valid = (
            completed == []
            and record.get("staged_f4_sha256") is None
            and record.get("staged_outcome_sha256") is None
            and staging
            in (
                {"unreserved", "released"}
                if terminal_failure
                else {"unreserved", "reserved", "active"}
            )
            and not (
                record.get("state")
                in {"child-barrier", "child-running", "child-terminal"}
                and staging != "active"
            )
        )
    else:
        prefix = (
            ["f4-decision", "final-freeze"]
            if record.get("state") == "success"
            else ["f4-decision"]
        )
        valid = (
            stage == "final-freeze"
            and completed == prefix
            and kernel.is_sha256(record.get("staged_f4_sha256"))
            and kernel.is_sha256(record.get("staged_outcome_sha256"))
            and record.get("final_gate_staging_state") == "released"
        )
    if not valid:
        kernel.fail("final controller staging matrix is invalid")


def _validate_child(record: JsonObject, state: JsonValue) -> None:
    identity = tuple(
        record.get(key) for key in ("child_pid", "child_pgid", "child_start_ticks")
    )
    active = state in {"child-barrier", "child-running", "child-terminal", "success"}
    if active and (
        not all(kernel.positive(value) for value in identity)
        or not isinstance(started := record.get("started_at_utc"), str)
        or not primitives.is_timestamp(started)
    ):
        kernel.fail("final-wave child identity is incomplete")
    barrier = record.get("child_barrier_released")
    if (state == "child-barrier" and barrier is not False) or (
        state in {"child-running", "child-terminal", "success"} and barrier is not True
    ):
        kernel.fail("final-wave child barrier matrix is invalid")
    kind = record.get("termination_kind")
    if state in {"child-terminal", "success"} and kind != "wait-result":
        kernel.fail("final-wave terminal child lacks a wait result")
    typed_outcome = record.get("typed_outcome_sha256")
    if kind == "wait-result" and (
        (record.get("wait_exit_code") is None) == (record.get("wait_signal") is None)
        or record.get("boot_disappearance_sha256") is not None
        or (typed_outcome is not None and not kernel.is_sha256(typed_outcome))
    ):
        kernel.fail("final-wave wait result is invalid")
    if kind == "boot-disappearance" and (
        not kernel.is_uuid(record.get("recovery_boot_id"))
        or not kernel.is_sha256(record.get("boot_disappearance_sha256"))
        or any(
            record.get(key) is not None
            for key in ("wait_exit_code", "wait_signal", "typed_outcome_sha256")
        )
        or record.get("timed_out") is not False
    ):
        kernel.fail("final-wave boot-disappearance matrix is invalid")
    if state == "success" and not kernel.successful_wait(record):
        kernel.fail("final-wave success requires a successful child outcome")


def _clear_final_wave_child(record: JsonObject) -> None:
    for match in re.finditer(
        r"[a-z0-9_]+",
        """started_at_utc child_pid child_pgid child_start_ticks
        child_barrier_released termination_kind boot_disappearance_sha256
        wait_exit_code wait_signal typed_outcome_sha256""",
    ):
        record[match.group()] = None
    record["timed_out"] = False


validate_final_wave_start = _validate_final_wave_start
initial_final_wave_record = _initial_final_wave_record
validate_final_wave_record = _validate_final_wave_record
clear_final_wave_child = _clear_final_wave_child
