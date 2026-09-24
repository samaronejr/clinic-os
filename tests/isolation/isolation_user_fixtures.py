from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import rfc8785
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    JsonObject,
    canonical_bytes,
    canonical_sha256,
    load_json,
    raw_sha256,
    stat_identity,
    write_no_replace,
)
from ops.testing.isolation_terminal_publisher_journal import JOURNAL_KEYS

from isolation_claim_fixtures import FOUNDATION_SHA, SECOND_CLAIM_ID, snapshot

TREE_SHA = "c" * 40
APPROVALS_SHA = "b" * 64


@dataclass(frozen=True, slots=True)
class UserGateFixture:
    ledger_path: Path
    control_root: Path
    terminal_path: Path
    final_sha256: str
    approvals_sha256: str
    tree_sha: str
    publisher_journal_sha256: str


@dataclass(frozen=True, slots=True)
class PublisherJournalInputs:
    ledger: JsonObject
    ledger_sha: str
    control_root: Path
    lock_path: Path
    inputs_sha: str
    pre_f4_sha: str
    final_sha: str


def user_gate_fixture(tmp_path: Path) -> UserGateFixture:
    ledger_path = snapshot(tmp_path)
    ledger, ledger_raw = load_json(ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    control_root = ledger_path.parent / "clinic-os-phase1a-final"
    terminal_root = control_root / "terminal"
    terminal_root.mkdir(parents=True, mode=0o700)
    lock_path = control_root / "final.lock"
    write_no_replace(lock_path, b"", mode=MODE_PRIVATE)
    inputs_raw = canonical_bytes({"phase": "inputs"})
    pre_f4_raw = canonical_bytes({"phase": "pre-f4"})
    final_raw = canonical_bytes(
        {
            "approvals_sha256": APPROVALS_SHA,
            "sha": FOUNDATION_SHA,
            "tree_sha": TREE_SHA,
        }
    )
    write_no_replace(
        terminal_root / "inputs.json",
        inputs_raw,
        mode=MODE_IMMUTABLE,
    )
    write_no_replace(
        control_root / "pre-f4.json",
        pre_f4_raw,
        mode=MODE_IMMUTABLE,
    )
    final_path = control_root / "final.json"
    write_no_replace(final_path, final_raw, mode=MODE_IMMUTABLE)
    journal = _released_publisher_journal(
        PublisherJournalInputs(
            ledger,
            raw_sha256(ledger_raw),
            control_root,
            lock_path,
            raw_sha256(inputs_raw),
            raw_sha256(pre_f4_raw),
            raw_sha256(final_raw),
        )
    )
    journal_path = attempt_root / "final-wave-state.json"
    journal_raw = canonical_bytes(journal)
    write_no_replace(journal_path, journal_raw, mode=MODE_PRIVATE)
    terminal_path = _terminal_revalidation(
        ledger,
        ledger_raw,
        control_root,
        raw_sha256(final_raw),
        raw_sha256(journal_raw),
    )
    return UserGateFixture(
        ledger_path=ledger_path,
        control_root=control_root,
        terminal_path=terminal_path,
        final_sha256=raw_sha256(final_raw),
        approvals_sha256=APPROVALS_SHA,
        tree_sha=TREE_SHA,
        publisher_journal_sha256=raw_sha256(journal_raw),
    )


def _released_publisher_journal(
    inputs: PublisherJournalInputs,
) -> JsonObject:
    ledger = inputs.ledger
    current_boot = str(ledger["boot_id"])
    journal: JsonObject = {
        "attempt_id": ledger["attempt_id"],
        "bootstrap_root": str(
            Path(str(ledger["attempt_root"])) / ("final-control-bootstrap-" + "a" * 32)
        ),
        "control_root": str(inputs.control_root),
        "control_root_identity": _compact_identity(inputs.control_root),
        "f4_sha256": "4" * 64,
        "final_sha256": inputs.final_sha,
        "inputs_sha256": inputs.inputs_sha,
        "lineage_validation_sha256": "e" * 64,
        "lock_identity": _compact_identity(inputs.lock_path),
        "phase": "final-frozen",
        "pre_f4_sha256": inputs.pre_f4_sha,
        "receipt_lineage_sha256": "f" * 64,
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "terminal_publisher_claim_id": SECOND_CLAIM_ID,
        "terminal_publisher_post_release_ledger_sha256": inputs.ledger_sha,
        "terminal_publisher_post_reservation_ledger_sha256": "1" * 64,
        "terminal_publisher_pre_release_ledger_sha256": "2" * 64,
        "terminal_publisher_pre_reservation_ledger_sha256": "3" * 64,
        "terminal_publisher_release_authorizations_sha256": "4" * 64,
        "terminal_publisher_release_basis_sha256": APPROVALS_SHA,
        "terminal_publisher_release_boot_id": current_boot,
        "terminal_publisher_release_context": "same-boot",
        "terminal_publisher_release_kind": "approved-chain",
        "terminal_publisher_released_at_utc": "2026-07-16T23:00:00.000001Z",
        "terminal_publisher_reservation_at_utc": "2026-07-16T22:00:00.000001Z",
        "terminal_publisher_reservation_spec_sha256": "5" * 64,
        "terminal_publisher_state": "released",
        "updated_at_utc": "2026-07-16T23:00:00.000001Z",
    }
    assert set(journal) == JOURNAL_KEYS
    return journal


def _terminal_revalidation(
    ledger: JsonObject,
    ledger_raw: bytes,
    control_root: Path,
    final_sha: str,
    journal_sha: str,
) -> Path:
    current_boot = str(ledger["boot_id"])
    observation = cast("JsonObject", ledger["boot_observation"])
    baseline = cast("JsonObject", ledger["baseline"])
    shared = cast("JsonObject", baseline["shared_evidence_manifest"])
    artifact: JsonObject = {
        "accepted_close_prepared_sha256": None,
        "approvals_sha256": APPROVALS_SHA,
        "attempt_id": ledger["attempt_id"],
        "boot_changed": False,
        "boot_observation": observation,
        "boot_observation_sha256": raw_sha256(rfc8785.dumps(observation)),
        "current_boot_id": current_boot,
        "final_relative_path": "clinic-os-phase1a-final/final.json",
        "final_sha256": final_sha,
        "lock_identity_sha256": canonical_sha256(ledger["lock_identity"]),
        "post_update_ledger_sha256": raw_sha256(ledger_raw),
        "pre_update_ledger_sha256": "6" * 64,
        "previous_boot_id": current_boot,
        "publisher_final_journal_sha256": journal_sha,
        "purpose": "terminal-final",
        "reboot_stable_baseline_sha256": ledger["reboot_stable_baseline_sha256"],
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "shared_evidence_manifest_sha256": shared["sha256"],
        "tree_sha": TREE_SHA,
        "verified_at_utc": observation["observed_at_utc"],
    }
    root = Path(str(ledger["attempt_root"])) / "terminal-revalidation"
    root.mkdir(mode=0o700)
    path = root / f"{current_boot}.json"
    write_no_replace(path, canonical_bytes(artifact), mode=MODE_IMMUTABLE)
    return path


def _compact_identity(path: Path) -> JsonObject:
    identity = stat_identity(path)
    identity.pop("link_count")
    return identity
