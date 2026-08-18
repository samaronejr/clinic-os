from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import isolation_common as c
from ops.testing import isolation_controller_kernel as kernel
from ops.testing import isolation_final_wave_controller as f
from ops.testing import isolation_final_wave_record as final_record
from ops.testing import isolation_review_lane_controller as r

from isolation_final_wave_engine_fixtures import (
    completed_review,
    review_start,
    schema_result,
    sealed_review,
)
from isolation_final_wave_engine_fixtures import (
    final_start as _final_start,
)

if TYPE_CHECKING:
    from pathlib import Path

ATTEMPT: Final = "11111111-1111-4111-8111-111111111111"
BOOT_A: Final = "22222222-2222-4222-8222-222222222222"
BOOT_B: Final = "33333333-3333-4333-8333-333333333333"
CLAIM: Final = "44444444-4444-4444-8444-444444444444"
SHA: Final = "a" * 40
DIGEST: Final = "c" * 64
NOW: Final = "2026-07-16T12:00:00.000000Z"
LATER: Final = "2026-07-16T12:01:00.000000Z"
child = kernel.ChildIdentity
waited = kernel.WaitResult
ALIVE: Final = kernel.RecoveryPolicy(lambda _pid, _ticks: True)
DEAD: Final = kernel.RecoveryPolicy(lambda _pid, _ticks: False)
TRUE: Final = True
FALSE: Final = False


def test_final_controller_locks_and_same_boot_takeover(tmp_path: Path) -> None:
    # Given: one owner holding the fixed final-wave lease.
    request = _final_start(tmp_path, BOOT_A, 2100)
    first = f.acquire_final_wave(request, ALIVE)

    # When / Then: a concurrent owner cannot publish another journal owner.
    with pytest.raises(c.IsolationError, match="lease"):
        _ = f.acquire_final_wave(_final_start(tmp_path, BOOT_A, 2200), ALIVE)
    first.abandon()

    # When: the recorded same-boot owner is provably absent.
    recovered = f.acquire_final_wave(
        _final_start(tmp_path, BOOT_A, 2200),
        kernel.RecoveryPolicy(lambda pid, ticks: (pid, ticks) != (2100, 6100)),
    )

    # Then: takeover is append-only and explicitly classified.
    owners = recovered.record["controller_owners"]
    assert isinstance(owners, list)
    prior, current = owners
    assert isinstance(prior, dict)
    assert isinstance(current, dict)
    assert prior["relinquish_kind"] == "stale-owner-recovery"
    assert current["pid"] == 2200
    assert recovered.record["state"] == "recovering"
    recovered.abandon()


def test_final_controller_reboot_never_probes_prior_pid(tmp_path: Path) -> None:
    # Given: a crash-durable final controller from an earlier boot.
    first = f.acquire_final_wave(_final_start(tmp_path, BOOT_A, 2300), ALIVE)
    first.abandon()

    # When: stale-boot recovery takes ownership.
    def forbidden_probe(_pid: int, _ticks: int) -> bool:
        message = "prior-boot process identity was probed"
        raise AssertionError(message)

    recovered = f.acquire_final_wave(
        _final_start(tmp_path, BOOT_B, 2400),
        kernel.RecoveryPolicy(forbidden_probe, boot_disappearance_sha256=DIGEST),
    )

    # Then: it records boot disappearance with no invented wait result.
    assert recovered.record["recovery_boot_id"] == BOOT_B
    assert recovered.record["termination_kind"] == "boot-disappearance"
    assert recovered.record["wait_exit_code"] is None
    assert recovered.record["wait_signal"] is None
    recovered.abandon()


def test_review_lanes_use_independent_fixed_leases(tmp_path: Path) -> None:
    # Given: independent F1 and F2 controller paths.
    f1 = r.acquire_review_lane(review_start(tmp_path, "F1", 2500), ALIVE)
    f2 = r.acquire_review_lane(review_start(tmp_path, "F2", 2600), ALIVE)

    # When / Then: both can hold their exact lease without shared mutable state.
    assert f1.record["lane"] == "F1"
    assert f2.record["lane"] == "F2"
    assert f1.record["controller_lease_path"] != f2.record["controller_lease_path"]
    f1.abandon()
    f2.abandon()


def test_publisher_release_requires_sealed_journal_receipt(tmp_path: Path) -> None:
    # Given: no immutable common receipt for a failure-ready controller.
    journal = tmp_path / "final-wave-controller.json"
    _ = journal.write_bytes(b"{}\n")
    journal.chmod(c.MODE_IMMUTABLE)

    # When / Then: publisher release cannot proceed on journal bytes alone.
    with pytest.raises(c.IsolationError, match="receipt"):
        _ = f.require_failure_receipt(journal, tmp_path / "ORCH.json")


def test_final_controller_rejects_tampered_final_form_matrix(tmp_path: Path) -> None:
    # Given: a durable final-form journal whose staging claim was erased.
    request = _final_start(tmp_path, BOOT_A, 2700, "final")
    session = f.acquire_final_wave(request, ALIVE)
    record = session.record
    session.abandon()
    record["final_gate_staging_claim_id"] = None
    c.write_atomic_replace(request.journal_path, c.canonical_bytes(record))

    # When / Then: recovery rejects the closed-root but illegal form matrix.
    with pytest.raises(c.IsolationError, match="staging"):
        _ = f.acquire_final_wave(request, DEAD)


def test_review_failure_cannot_advance_to_the_next_stage(tmp_path: Path) -> None:
    # Given: F2 environment sync terminated with a typed nonzero outcome.
    session = r.acquire_review_lane(review_start(tmp_path, "F2", 2800), ALIVE)
    session.reserve_filesystems(NOW)
    session.activate_filesystems(r.ToolClosure(DIGEST, DIGEST), NOW)
    session.reserve_child_claim(CLAIM, DIGEST, NOW)
    session.record_child(r.ReviewChild(CLAIM, DIGEST, child(2801, 2801, 6801)), NOW)
    session.release_child(NOW)
    session.record_wait(waited(1, None, FALSE, DIGEST), NOW)

    # When: the controller archives the failed child stage.
    session.advance_stage(NOW, DIGEST)

    # Then: no successor stage becomes runnable.
    assert session.record["state"] == "recovering"
    assert session.record["stage"] == "environment-sync"
    session.abandon()


def test_final_controller_renews_held_fixed_lease(tmp_path: Path) -> None:
    # Given: one final-wave owner holds the authenticated fixed lease.
    session = f.acquire_final_wave(_final_start(tmp_path, BOOT_A, 2900), ALIVE)

    # When: that owner renews its durable heartbeat.
    session.renew(LATER)

    # Then: only the update timestamp advances and the owner remains held.
    assert session.record["updated_at_utc"] == LATER
    assert session.record["controller_lease_state"] == "held"
    session.abandon()


def test_final_controller_gates_final_stage_on_publisher(tmp_path: Path) -> None:
    # Given: a final-form owner without a reserved staging claim.
    request = _final_start(tmp_path, BOOT_A, 2900, "final")
    session = f.acquire_final_wave(request, ALIVE)
    with pytest.raises(c.IsolationError, match="staging"):
        session.record_child(child(2901, 2901, 6901), LATER)
    session.reserve_final_staging(NOW)
    assert session.record["final_gate_staging_state"] == "reserved"
    session.abandon()

    # When: recovery preserves the reservation before activating its fresh root.
    recovery = replace(request, owner_pid=2902, owner_start_ticks=6902)
    session = f.acquire_final_wave(recovery, DEAD)
    assert session.record["state"] == "recovering"
    assert session.record["final_gate_staging_state"] == "reserved"
    session.activate_final_staging(LATER)
    assert session.record["final_gate_staging_state"] == "active"
    session.record_child(child(2901, 2901, 6901), LATER)
    session.release_child(LATER)
    session.record_wait(waited(0, None, FALSE, DIGEST), LATER)

    # When / Then: the freezer stage requires a durable publisher authorization.
    with pytest.raises(c.IsolationError, match="publisher"):
        session.advance_final_decision((DIGEST, DIGEST, None), LATER)
    assert session.record["final_gate_staging_state"] == "active"
    session.advance_final_decision((DIGEST, DIGEST, DIGEST), LATER)
    assert session.record["completed_stages"] == ["f4-decision"]
    assert session.record["stage"] == "final-freeze"
    assert session.record["final_gate_staging_state"] == "released"
    session.abandon()


def test_same_boot_takeover_resumes_the_final_freeze_stage(tmp_path: Path) -> None:
    # Given: final decision evidence is durable and only final-freeze remains.
    request = _final_start(tmp_path, BOOT_A, 2950, "final")
    first = f.acquire_final_wave(request, ALIVE)
    first.reserve_final_staging(NOW)
    first.activate_final_staging(NOW)
    first.record_child(child(2951, 2951, 6951), NOW)
    first.release_child(NOW)
    first.record_wait(waited(0, None, FALSE, DIGEST), NOW)
    first.advance_final_decision((DIGEST, DIGEST, DIGEST), LATER)
    first.abandon()

    # When: a proven-dead same-boot owner is replaced on the fixed lease.
    takeover = replace(
        request, owner_pid=2952, owner_start_ticks=6952, acquired_at_utc=LATER
    )
    recovered = f.acquire_final_wave(takeover, DEAD)

    # Then: the exact durable stage resumes and can seal successfully.
    assert recovered.record["state"] == "between-stages"
    assert recovered.record["completed_stages"] == ["f4-decision"]
    assert recovered.record["final_gate_staging_state"] == "released"
    recovered.record_child(child(2953, 2953, 6953), LATER)
    recovered.release_child(LATER)
    recovered.record_wait(waited(0, None, FALSE, DIGEST), LATER)
    recovered.seal_success(LATER)
    durable, _ = c.load_json(request.journal_path)
    final_record.validate_final_wave_record(durable)
    assert durable["state"] == "success"
    assert schema_result(tmp_path, 99, durable).returncode == 0


def test_review_success_requires_publisher_authorization(tmp_path: Path) -> None:
    # Given: F1 completed its sole review child successfully.
    session = r.acquire_review_lane(review_start(tmp_path, "F1", 3000), ALIVE)
    session.reserve_filesystems(NOW)
    session.activate_filesystems(r.ToolClosure(DIGEST, None), NOW)
    session.reserve_child_claim(CLAIM, DIGEST, NOW)
    session.record_child(r.ReviewChild(CLAIM, DIGEST, child(3001, 3001, 7001)), NOW)
    session.release_child(NOW)
    session.record_wait(waited(0, None, FALSE, DIGEST), NOW)
    session.advance_stage(NOW)

    # When / Then: terminal publication cannot be inferred from staged bytes.
    with pytest.raises(c.IsolationError, match="publisher"):
        session.seal(r.ReviewSeal(TRUE, LATER, DIGEST))
    session.seal(r.ReviewSeal(TRUE, LATER, DIGEST, DIGEST))


def test_final_controller_rejects_timed_out_false_success(tmp_path: Path) -> None:
    # Given: a child reported exit zero but also timed out with no typed result.
    session = f.acquire_final_wave(_final_start(tmp_path, BOOT_A, 3100), ALIVE)
    session.record_child(child(3101, 3101, 7101), NOW)
    session.release_child(NOW)
    session.record_wait(waited(0, None, TRUE, None), NOW)

    # When / Then: misleading exit status cannot seal controller success.
    with pytest.raises(c.IsolationError, match="outcome"):
        session.seal_success(LATER)
    session.abandon()


@pytest.mark.parametrize(
    ("exit_codes", "success"),
    [((0, 0, 1), False), ((0, 0, 0), True)],
)
def test_review_success_requires_every_completed_child_to_succeed(
    tmp_path: Path,
    exit_codes: tuple[int, int, int],
    success: bool,
) -> None:
    # Given: a complete F2 child history with the supplied exit vector.
    session = completed_review(tmp_path, "F2", exit_codes, 3300)

    # When / Then: one failed child blocks success while the all-green control seals.
    if success:
        session.seal(r.ReviewSeal(TRUE, LATER, DIGEST, DIGEST))
        assert session.record["state"] == "success"
    else:
        with pytest.raises(c.IsolationError, match="completed child"):
            session.seal(r.ReviewSeal(TRUE, LATER, DIGEST, DIGEST))
        session.abandon()


def test_failed_f2_blocks_pre_f4_and_final_predecessor_bindings(
    tmp_path: Path,
) -> None:
    # Given: F1 succeeded but F2 sealed after its final child failed.
    f1 = sealed_review(tmp_path / "f1", "F1", (0,), 3400, success=True)
    f2 = sealed_review(tmp_path / "f2", "F2", (0, 0, 1), 3500, success=False)
    evidence = final_record.PreF4Evidence(DIGEST, f1, f2, DIGEST, DIGEST)

    # When / Then: neither downstream form can authenticate the failed predecessor.
    for form in ("pre-f4", "final"):
        request = replace(
            _final_start(tmp_path / form, BOOT_A, 3600),
            form=form,
            final_gate_staging_claim_id=CLAIM if form == "final" else None,
            predecessor_evidence=evidence,
            pre_f4_sha256=DIGEST if form == "final" else None,
        )
        with pytest.raises(c.IsolationError, match="F2"):
            _ = f.acquire_final_wave(request, ALIVE)


def test_all_success_predecessors_allow_scope_pre_pre_f4_and_final(
    tmp_path: Path,
) -> None:
    # Given: exact successful F1/F2 records and the remaining approval hashes.
    f1 = sealed_review(tmp_path / "f1", "F1", (0,), 3700, success=True)
    f2 = sealed_review(tmp_path / "f2", "F2", (0, 0, 0), 3800, success=True)
    evidence = final_record.PreF4Evidence(DIGEST, f1, f2, DIGEST, DIGEST)

    # When: scope-pre starts independently and downstream forms bind the approvals.
    scope = f.acquire_final_wave(
        replace(_final_start(tmp_path / "scope", BOOT_A, 3900), form="scope-pre"),
        ALIVE,
    )
    pre_f4 = f.acquire_final_wave(
        replace(
            _final_start(tmp_path / "pre", BOOT_A, 4000),
            form="pre-f4",
            predecessor_evidence=evidence,
        ),
        ALIVE,
    )
    final = f.acquire_final_wave(
        replace(
            _final_start(tmp_path / "final", BOOT_A, 4100),
            form="final",
            final_gate_staging_claim_id=CLAIM,
            predecessor_evidence=evidence,
            pre_f4_sha256=DIGEST,
        ),
        ALIVE,
    )

    # Then: the two downstream journals share one authenticated evidence digest.
    assert scope.record["predecessor_evidence_sha256"] is None
    assert (
        pre_f4.record["predecessor_evidence_sha256"]
        == final.record["predecessor_evidence_sha256"]
    )
    for session in (scope, pre_f4, final):
        session.abandon()
