"""Crash-safe state transitions for final-wave controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from ops.testing import isolation_common as c
from ops.testing import isolation_controller_kernel as kernel
from ops.testing.isolation_final_wave_evidence import _predecessor_evidence_sha256
from ops.testing.isolation_final_wave_record import FinalForm as _FinalForm
from ops.testing.isolation_final_wave_record import (
    FinalWaveStart,
    clear_final_wave_child,
    initial_final_wave_record,
    validate_final_wave_record,
    validate_final_wave_start,
)

if TYPE_CHECKING:
    from pathlib import Path


@final
class _FinalWaveSession:
    __slots__ = ("_lease", "_path", "record")

    def __init__(
        self, lease: kernel.FixedLease, path: Path, record: c.JsonObject
    ) -> None:
        self._lease, self._path, self.record = lease, path, record

    def renew(self, now: str) -> None:
        if self._lease.descriptor is None:
            kernel.fail("final-wave lease is not held")
        self._update(now)

    def record_child(self, identity: kernel.ChildIdentity, now: str) -> None:
        if (
            self.record["state"] not in {"prepared", "between-stages"}
            or min(identity.pid, identity.pgid, identity.start_ticks) < 1
        ):
            kernel.fail("final-wave child cannot start")
        if self.record["form"] == "final" and self.record[
            "final_gate_staging_state"
        ] != ("active" if self.record["stage"] == "f4-decision" else "released"):
            kernel.fail("final-wave child lacks its staging lifecycle authority")
        clear_final_wave_child(self.record)
        self._update(
            now,
            state="child-barrier",
            started_at_utc=now,
            child_pid=identity.pid,
            child_pgid=identity.pgid,
            child_start_ticks=identity.start_ticks,
            child_barrier_released=False,
        )

    def release_child(self, now: str) -> None:
        self._expect("child-barrier")
        self._update(now, state="child-running", child_barrier_released=True)

    def record_wait(self, result: kernel.WaitResult, now: str) -> None:
        self._expect("child-running")
        kernel.validate_wait(result)
        self._update(
            now,
            state="child-terminal",
            termination_kind="wait-result",
            wait_exit_code=result.exit_code,
            wait_signal=result.signal,
            timed_out=result.timed_out,
            typed_outcome_sha256=result.typed_outcome_sha256,
        )

    def advance_final_decision(
        self, evidence: tuple[str, str, str | None], now: str
    ) -> None:
        decision, outcome, publisher = evidence
        if (self.record["form"], self.record["stage"]) != ("final", "f4-decision"):
            kernel.fail("final decision belongs to another stage")
        if not kernel.successful_wait(self.record):
            kernel.fail("final decision lacks a successful typed outcome")
        if self.record["final_gate_staging_state"] != "active":
            kernel.fail("final decision staging is not active")
        if (
            not kernel.is_sha256(decision)
            or outcome != self.record["typed_outcome_sha256"]
            or publisher != decision
        ):
            kernel.fail("final decision lacks matching publisher authorization")
        clear_final_wave_child(self.record)
        self._update(
            now,
            state="between-stages",
            stage="final-freeze",
            completed_stages=["f4-decision"],
            final_gate_staging_state="released",
            staged_f4_sha256=decision,
            staged_outcome_sha256=outcome,
        )

    def seal_failure(self, now: str) -> None:
        if self.record["state"] not in {"child-terminal", "recovering"}:
            kernel.fail("final-wave failure has no terminal authority")
        if self.record["form"] == "final" and self.record[
            "final_gate_staging_state"
        ] in {"reserved", "active"}:
            self.record["final_gate_staging_state"] = "released"
        self._seal("failure-ready", now)

    def prepare_failure(self, now: str) -> None:
        if self.record["state"] in {"failure-ready", "success"}:
            kernel.fail("terminal final-wave controller cannot prepare failure")
        clear_final_wave_child(self.record)
        self._update(now, state="recovering")

    def seal_success(self, now: str) -> None:
        if self.record["state"] != "child-terminal" or not kernel.successful_wait(
            self.record
        ):
            kernel.fail("final-wave success lacks a successful typed outcome")
        if self.record["form"] == "final":
            if self.record["completed_stages"] != ["f4-decision"]:
                kernel.fail("final-wave success lacks its publisher-gated decision")
            self.record["completed_stages"] = ["f4-decision", "final-freeze"]
        self._seal("success", now)

    def abandon(self) -> None:
        self._lease.close()

    def reserve_final_staging(self, now: str) -> None:
        self._transition_final_staging("unreserved", "reserved", now)

    def activate_final_staging(self, now: str) -> None:
        current = self.record["final_gate_staging_state"]
        if current not in {"reserved", "active"}:
            kernel.fail("final staging activation has no reservation")
        self._transition_final_staging(current, "active", now)

    def _transition_final_staging(self, before: str, after: str, now: str) -> None:
        if (
            (self.record["form"], self.record["stage"]) != ("final", "f4-decision")
            or self.record["state"] not in {"prepared", "recovering"}
            or self.record["final_gate_staging_state"] != before
        ):
            kernel.fail("final staging lifecycle transition is invalid")
        clear_final_wave_child(self.record)
        self._update(now, state="prepared", final_gate_staging_state=after)

    def _expect(self, state: str) -> None:
        if self.record["state"] != state:
            kernel.fail(f"final-wave controller is not {state}")

    def _update(self, now: str, **changes: c.JsonValue) -> None:
        self.record.update(changes)
        self.record["updated_at_utc"] = now
        validate_final_wave_record(self.record)
        c.write_atomic_replace(self._path, c.canonical_bytes(self.record))

    def _seal(self, state: str, now: str) -> None:
        kernel.close_controller_owner(self.record, now)
        self.record.update(
            {
                "state": state,
                "controller_lease_state": "released",
                "cleanup_verified": True,
                "updated_at_utc": now,
            }
        )
        validate_final_wave_record(self.record)
        kernel.seal_controller_record(self._path, self.record)
        self._lease.close()


def _acquire_final_wave(
    request: FinalWaveStart, recovery: kernel.RecoveryPolicy
) -> _FinalWaveSession:
    validate_final_wave_start(request)
    predecessor_sha256 = _predecessor_evidence_sha256(request)
    lease = kernel.acquire_fixed_lease(request.lease_path)
    ready = False
    try:
        if request.journal_path.exists():
            _ = c.regular_identity(request.journal_path, mode=c.MODE_PRIVATE)
            record, _ = c.load_json(request.journal_path)
            validate_final_wave_record(record)
            binding = (
                record.get("attempt_id"),
                record.get("sha"),
                record.get("form"),
                record.get("predecessor_evidence_sha256"),
                record.get("pre_f4_sha256"),
                record.get("final_gate_staging_claim_id"),
                record.get("controller_lease_identity"),
            )
            expected = (
                request.attempt_id,
                request.sha,
                request.form,
                predecessor_sha256,
                request.pre_f4_sha256,
                request.final_gate_staging_claim_id,
                lease.identity,
            )
            if binding != expected:
                kernel.fail("final-wave invocation or lease binding drifted")
            resume_final_freeze = record["stage"] == "final-freeze"
            resume_final_freeze &= record["state"] == "between-stages"
            resume_final_freeze &= record["creation_boot_id"] == request.boot_id
            kernel.recover_controller_owner(record, request, recovery)
            if resume_final_freeze:
                record["state"] = "between-stages"
            c.write_atomic_replace(request.journal_path, c.canonical_bytes(record))
        else:
            record = initial_final_wave_record(
                request, lease.identity, predecessor_sha256
            )
            c.write_no_replace(
                request.journal_path,
                c.canonical_bytes(record),
                mode=c.MODE_PRIVATE,
            )
        ready = True
        return _FinalWaveSession(lease, request.journal_path, record)
    finally:
        if not ready:
            lease.close()


FinalForm, FinalWaveSession = _FinalForm, _FinalWaveSession
acquire_final_wave = _acquire_final_wave
require_failure_receipt = kernel.require_failure_receipt
