from __future__ import annotations

import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import cgroup_probe_recovery as recovery
from ops.testing.cgroup_capability_probe import ProbeRequest
from ops.testing.cgroup_probe_records import _initial_journal
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_stale_recovery import reconcile_stale_boot

from isolation.isolation_rejection_fixtures import empty_inventory
from isolation_claim_fixtures import snapshot

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession

ATTEMPT_ID: Final = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PREVIOUS_BOOT: Final = "11111111-1111-4111-8111-111111111111"
CURRENT_BOOT: Final = "22222222-2222-4222-8222-222222222222"
PROBE_PID: Final = 2_000_000_001
ACTIVE_STATES: Final = (
    "prepared",
    "mkdir-intent",
    "child-ready",
    "probe-identity",
    "probe-migrated",
    "probe-running",
    "kill-complete",
    "remove-intent",
)
PROCESS_STATES: Final = frozenset(
    {
        "probe-identity",
        "probe-migrated",
        "probe-running",
        "kill-complete",
        "remove-intent",
    }
)


def test_stale_recovery_seals_every_probe_prefix_without_signaling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: both probe purposes stopped at every durable prior-boot state.
    process_checks: list[tuple[int, int]] = []

    def process_absent(pid: int, start_ticks: int) -> bool:
        process_checks.append((pid, start_ticks))
        return False

    monkeypatch.setattr(recovery, "_matching_process", process_absent)
    monkeypatch.setattr(
        recovery,
        "_write_control",
        lambda *_args: pytest.fail("stale recovery signaled the new cgroup tree"),
    )

    # When: stale reconciliation proves disappearance for every prefix.
    recovered_paths: list[Path] = []
    for purpose in ("todo1-kickoff", "final-input-freeze"):
        for state in ACTIVE_STATES:
            case_root = tmp_path / purpose / state
            request, path, child = _write_prefix(case_root, purpose, state)
            recovery.recover_stale_boot_probe(path, request, CURRENT_BOOT)
            recovered_paths.append(path)
            assert not child.exists()

    # Then: each journal is immutable and records only boot disappearance.
    assert len(recovered_paths) == len(ACTIVE_STATES) * 2
    for path in recovered_paths:
        journal, _ = load_json(path)
        assert journal["state"] == "removed"
        assert journal["recovery_boot_id"] == CURRENT_BOOT
        assert journal["removal_kind"] == "boot-disappearance"
        assert stat.S_IMODE(path.stat().st_mode) == MODE_IMMUTABLE
        recovery.recover_stale_boot_probe(
            path,
            _request_for(path),
            CURRENT_BOOT,
        )
    assert process_checks == [(PROBE_PID, 9_999)] * (len(PROCESS_STATES) * 2)


def test_stale_recovery_blocks_a_live_prior_identity_without_signaling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a prior-boot probe identity still resolves to the recorded process.
    request, path, _child = _write_prefix(tmp_path, "todo1-kickoff", "probe-identity")
    before = path.read_bytes()
    signals: list[Path] = []
    monkeypatch.setattr(recovery, "_matching_process", lambda *_args: True)
    monkeypatch.setattr(
        recovery,
        "_write_control",
        lambda target, _raw: signals.append(target),
    )

    # When: the changed-boot cleanup attempts to authenticate disappearance.
    with pytest.raises(IsolationError, match="prior-boot probe process"):
        recovery.recover_stale_boot_probe(path, request, CURRENT_BOOT)

    # Then: no signal, journal transition, or child-tree mutation occurs.
    assert signals == []
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == MODE_PRIVATE


def test_stale_coordinator_closes_probe_before_outer_journal_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a stale canonical ledger whose kickoff probe still names a live PID.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    probe_path = attempt_root / "execution-host-probes/todo1-kickoff.json"
    journal, _ = load_json(probe_path)
    journal.update(
        {
            "creation_boot_id": PREVIOUS_BOOT,
            "probe_barrier_released": False,
            "recovery_boot_id": None,
            "removal_kind": None,
            "removed_at_utc": None,
            "state": "probe-identity",
        }
    )
    probe_path.chmod(MODE_PRIVATE)
    write_atomic_replace(probe_path, canonical_bytes(journal))
    ledger["boot_id"] = PREVIOUS_BOOT
    observation = ledger["boot_observation"]
    assert isinstance(observation, dict)
    observation["boot_id"] = PREVIOUS_BOOT
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    monkeypatch.setattr(recovery, "_matching_process", lambda *_args: True)

    # When: the sole changed-boot command enters with that unresolved identity.
    with pytest.raises(IsolationError, match="prior-boot probe process"):
        reconcile_stale_boot(
            ledger_path,
            inventory_reader=empty_inventory,
            proof_publisher=_reuse_current_proof,
        )

    # Then: no outer recovery journal or receipt-producing cleanup has started.
    assert not (attempt_root / "stale-boot-recovery.json").exists()
    assert probe_path.read_bytes() == canonical_bytes(journal)


def _write_prefix(
    root: Path,
    purpose: str,
    state: str,
) -> tuple[ProbeRequest, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    attempt_root = root / ATTEMPT_ID
    attempt_root.mkdir()
    parent = root / "delegated-parent"
    parent.mkdir()
    parent_value = parent.stat()
    identity: JsonObject = {
        "device": parent_value.st_dev,
        "gid": os.getegid(),
        "inode": parent_value.st_ino,
        "uid": os.geteuid(),
    }
    proof: JsonObject = {
        "boot_id": PREVIOUS_BOOT,
        "cgroup_parent_identity": identity,
        "cgroup_parent_path": str(parent),
        "cgroup_relative_path": "/delegated-parent",
    }
    proof_raw = canonical_bytes(proof)
    proof_path = root / "proof.json"
    write_no_replace(proof_path, proof_raw, mode=MODE_IMMUTABLE)
    request = ProbeRequest(
        ATTEMPT_ID,
        attempt_root,
        proof_path,
        raw_sha256(proof_raw),
        purpose,
    )
    child = parent / f"clinic-os-phase1a-probe-{ATTEMPT_ID}-{purpose}"
    journal = _initial_journal(request, parent, identity, child)
    journal["creation_boot_id"] = PREVIOUS_BOOT
    if state not in {"prepared", "mkdir-intent"}:
        child.mkdir()
        child_value = child.stat()
        journal["child_device"] = child_value.st_dev
        journal["child_inode"] = child_value.st_ino
        child.rmdir()
    if state in PROCESS_STATES:
        journal.update(
            {
                "expected_parent_pid": 1,
                "probe_barrier_released": state
                not in {"probe-identity", "probe-migrated"},
                "probe_pgid": PROBE_PID,
                "probe_pid": PROBE_PID,
                "probe_start_ticks": 9_999,
            }
        )
    journal["state"] = state
    path = attempt_root / f"execution-host-probes/{purpose}.json"
    path.parent.mkdir()
    write_no_replace(path, canonical_bytes(journal), mode=MODE_PRIVATE)
    return request, path, child


def _request_for(path: Path) -> ProbeRequest:
    journal, _ = load_json(path)
    attempt_root = path.parents[1]
    return ProbeRequest(
        str(journal["attempt_id"]),
        attempt_root,
        attempt_root.parent.parent / "proof.json",
        str(journal["proof_sha256"]),
        str(journal["purpose"]),
    )


def _reuse_current_proof(
    session: LedgerSession,
    _journal: JsonObject,
) -> JsonObject:
    proof = session.ledger["execution_host_preflight"]
    assert isinstance(proof, dict)
    return deepcopy(proof)
