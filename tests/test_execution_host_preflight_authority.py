from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from typing import TYPE_CHECKING

import pytest
from ops.testing.execution_host_resume_authority import JOURNAL_KEYS
from ops.testing.isolation_common import (
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
)

from execution_host_preflight_harness import BOOT_ID, PreflightHarness

if TYPE_CHECKING:
    from pathlib import Path

PREVIOUS_BOOT = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.parametrize("marker", ["state", "sentinel"])
def test_preexisting_archive_marker_refuses_every_proof_name(
    tmp_path: Path,
    marker: str,
) -> None:
    # Given: one stable archive marker exists before ordinary publication.
    harness = PreflightHarness.create(tmp_path)
    marker_path = (
        harness.archive_state if marker == "state" else harness.archive_sentinel
    )
    marker_path.write_bytes(b"active")

    # When / Then: the committed publisher fails before creating either proof name.
    with pytest.raises(SystemExit, match="archive rollover is active"):
        harness.run()
    assert not harness.artifact.exists()
    assert not harness.pending.exists()


def test_malformed_immutable_pending_fails_unchanged(tmp_path: Path) -> None:
    # Given: a mode-0400 pending inode with nonconforming proof bytes.
    harness = PreflightHarness.create(tmp_path)
    harness.pending.write_bytes(b"{}\n")
    harness.pending.chmod(0o400)
    identity = harness.pending.stat()

    # When / Then: validation refuses it without a writable reopen or link mutation.
    with pytest.raises(SystemExit, match="proof has the wrong closed key set"):
        harness.run()
    assert harness.pending.read_bytes() == b"{}\n"
    assert harness.pending.stat().st_ino == identity.st_ino
    assert stat.S_IMODE(harness.pending.stat().st_mode) == 0o400
    assert not harness.artifact.exists()


def test_ordinary_form_refuses_an_open_canonical_ledger(tmp_path: Path) -> None:
    # Given: the canonical ledger name is still present for an ordinary invocation.
    harness = PreflightHarness.create(tmp_path)
    ledger = harness.authority_root / "evidence/isolation-ledger-phase1a.json"
    ledger.write_bytes(b"{}\n")
    ledger.chmod(0o600)

    # When / Then: ordinary publication cannot use the stale-resume exception.
    with pytest.raises(SystemExit, match="ordinary proof publication requires"):
        harness.run()
    assert not harness.artifact.exists()
    assert not harness.pending.exists()


def test_stale_resume_form_uses_exact_journal_and_inherited_lock_authority(
    tmp_path: Path,
) -> None:
    # Given: a canonical claims-pruned resume journal and its already-held stable lock.
    harness = PreflightHarness.create(tmp_path)
    ledger_path, journal_path = _write_stale_resume_authority(harness)
    harness.create_stable_lock()
    descriptor = os.open(
        harness.stable_lock,
        os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # When: the special publisher form duplicates and reasserts that lock.
        digest = harness.run(
            stale_journal=journal_path,
            stable_lock_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)

    # Then: it retains the old ledger and publishes only the current-boot proof.
    assert digest == hashlib.sha256(harness.artifact.read_bytes()).hexdigest()
    assert _json_boot_id(harness.artifact) == BOOT_ID
    assert ledger_path.exists()
    assert stat.S_IMODE(harness.artifact.stat().st_mode) == 0o400
    assert not harness.pending.exists()


def _write_stale_resume_authority(
    harness: PreflightHarness,
) -> tuple[Path, Path]:
    attempt_root = harness.authority_root / f"evidence/runtime/{ATTEMPT_ID}"
    attempt_root.mkdir(parents=True)
    ledger: JsonObject = {
        "attempt_id": ATTEMPT_ID,
        "attempt_root": str(attempt_root),
        "boot_id": PREVIOUS_BOOT,
        "claims": [],
    }
    ledger_raw = canonical_bytes(ledger)
    ledger_path = harness.authority_root / "evidence/isolation-ledger-phase1a.json"
    ledger_path.write_bytes(ledger_raw)
    ledger_path.chmod(0o600)
    ledger_sha = raw_sha256(ledger_raw)
    journal: JsonObject = dict.fromkeys(JOURNAL_KEYS)
    journal.update(
        {
            "attempt_id": ATTEMPT_ID,
            "completed_action_ids": [],
            "controller_recoveries": [],
            "current_boot_id": BOOT_ID,
            "f3_recovery_required": False,
            "post_cleanup_ledger_sha256": ledger_sha,
            "previous_boot_id": PREVIOUS_BOOT,
            "prior_ledger_sha256": ledger_sha,
            "recovery_goal": "resume",
            "resource_actions": [],
            "runner_recoveries": [],
            "schema_version": 1,
            "state": "claims-pruned",
            "updated_at_utc": "2026-07-16T12:34:56.123456Z",
        }
    )
    journal_path = attempt_root / "stale-boot-recovery.json"
    journal_path.write_bytes(canonical_bytes(journal))
    journal_path.chmod(0o600)
    return ledger_path, journal_path


def _json_boot_id(path: Path) -> object:
    value, _raw = load_json(path)
    return value.get("boot_id")
