from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import rfc8785
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    stat_identity,
    utc_now,
    write_atomic_replace,
    write_no_replace,
)

from isolation_claim_fixtures import FOUNDATION_SHA, SECOND_CLAIM_ID, snapshot

if TYPE_CHECKING:
    from _typeshed import StrPath

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"


def stale_publisher_ledger(tmp_path: Path, status: str) -> tuple[Path, Path]:
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    ledger["boot_id"] = PREVIOUS_BOOT
    observation = cast("JsonObject", ledger["boot_observation"])
    observation["boot_id"] = PREVIOUS_BOOT
    before_publisher = canonical_bytes(ledger)
    control_root, terminal_root, lock_path = _control_paths(ledger)
    desired = {
        "owned_files": [],
        "published_outputs": cast("JsonValue", _terminal_authorizations(terminal_root)),
    }
    claim = _publisher_claim(desired, status)
    reserved_claim = _publisher_claim(desired, "reserved")
    reserved_ledger = copy.deepcopy(ledger)
    reserved_ledger["claims"] = [reserved_claim]
    ledger["claims"] = [claim]
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    if status == "active":
        claim_root = Path(str(ledger["attempt_root"])) / str(
            claim["root_relative_path"]
        )
        claim_root.parent.mkdir(mode=0o700, exist_ok=True)
        claim_root.mkdir(mode=0o700)
    journal_path = Path(str(ledger["attempt_root"])) / "final-wave-state.json"
    journal = _final_wave_journal(
        ledger,
        claim,
        (before_publisher, canonical_bytes(reserved_ledger)),
        (control_root, lock_path),
    )
    write_no_replace(journal_path, canonical_bytes(journal), mode=MODE_PRIVATE)
    _write_failure_receipt(ledger)
    return ledger_path, journal_path


def _control_paths(ledger: JsonObject) -> tuple[Path, Path, Path]:
    attempt_root = Path(str(ledger["attempt_root"]))
    evidence_root = attempt_root.parents[1]
    control_root = evidence_root / "clinic-os-phase1a-final"
    terminal_root = control_root / "terminal"
    terminal_root.mkdir(parents=True, mode=0o700)
    lock_path = control_root / "final.lock"
    write_no_replace(lock_path, b"", mode=MODE_PRIVATE)
    return control_root, terminal_root, lock_path


def _publisher_claim(desired: JsonObject, status: str) -> JsonObject:
    active = status == "active"
    timestamp = "2026-07-16T21:00:00.000001Z"
    claim: JsonObject = {
        "activated_at_utc": "2026-07-16T21:00:02.000003Z" if active else None,
        "candidate_envelope_binding": None,
        "claim_id": SECOND_CLAIM_ID,
        "dependency_claim_ids": [],
        "desired": copy.deepcopy(desired),
        "kind": "filesystem",
        "last_verified_at_utc": "2026-07-16T21:00:03.000004Z",
        "observed": {
            "owned_files": [],
            "published_outputs": cast(
                "JsonValue",
                _unpublished_observations(desired) if active else [],
            ),
        },
        "prepared_at_utc": None,
        "purpose": "final-terminal-publisher",
        "reserved_at_utc": timestamp,
        "root_relative_path": f"claims/{SECOND_CLAIM_ID}",
        "runner_creation": None,
        "status": status,
    }
    return claim


def _terminal_authorizations(root: Path) -> list[JsonObject]:
    definitions: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("input-launcher-path", (), ("F3-launcher.path",)),
        ("input-launcher-lstat", ("input-launcher-path",), ("F3-launcher.lstat",)),
        (
            "input-launcher-sha256",
            ("input-launcher-lstat",),
            ("F3-launcher.sha256",),
        ),
        (
            "input-manifest",
            ("input-launcher-lstat", "input-launcher-path", "input-launcher-sha256"),
            ("inputs.json",),
        ),
        ("input-sidecar", ("input-manifest",), ("inputs.sha256",)),
        ("f1-verdict", (), ("F1-verdict.json",)),
        ("f1-receipt", ("f1-verdict",), ("F1-receipt.txt",)),
        ("f2-prerequisites", (), ("F2-prerequisites.json",)),
        ("f2-verdict", ("f2-prerequisites",), ("F2-verdict.json",)),
        ("f2-receipt", ("f2-verdict",), ("F2-receipt.txt",)),
        ("f3-artifacts", (), ("F3/runtime.json",)),
        ("f3-receipt", ("f3-artifacts",), ("F3/receipt.json",)),
        ("f3-manifest", ("f3-receipt",), ("F3/manifest.json",)),
        ("f4-pre", (), ("F4-pre.txt",)),
        ("f4-final", ("f4-pre",), ("F4-final.txt",)),
    )
    return [
        {
            "authorization_id": identifier,
            "gid": os.getegid(),
            "governing_lock": "final",
            "mode": MODE_IMMUTABLE,
            "output_kind": "final-terminal",
            "predecessor_authorization_ids": cast("JsonValue", list(predecessors)),
            "relative_paths": cast("JsonValue", list(paths)),
            "root_path": str(root),
            "uid": os.geteuid(),
        }
        for identifier, predecessors, paths in definitions
    ]


def _unpublished_observations(desired: JsonObject) -> list[JsonObject]:
    authorizations = cast("list[JsonObject]", desired["published_outputs"])
    return [
        {
            "authorization_id": item["authorization_id"],
            "entries": [],
            "governing_lock": item["governing_lock"],
            "output_kind": item["output_kind"],
            "root_path": item["root_path"],
            "status": "unpublished",
        }
        for item in authorizations
    ]


def _final_wave_journal(
    ledger: JsonObject,
    claim: JsonObject,
    reservation_bytes: tuple[bytes, bytes],
    control_paths: tuple[Path, Path],
) -> JsonObject:
    pre_reservation, post_reservation = reservation_bytes
    control_root, lock_path = control_paths
    spec = {
        "claim_id": claim["claim_id"],
        "dependency_claim_ids": claim["dependency_claim_ids"],
        "desired": claim["desired"],
        "kind": claim["kind"],
        "purpose": claim["purpose"],
    }
    return {
        "bootstrap_root": str(
            Path(str(ledger["attempt_root"])) / ("final-control-bootstrap-" + "a" * 32)
        ),
        "control_root": str(control_root),
        "control_root_identity": _compact_identity(control_root),
        "f4_sha256": None,
        "final_sha256": None,
        "inputs_sha256": None,
        "lineage_validation_sha256": "e" * 64,
        "lock_identity": _compact_identity(lock_path),
        "phase": "initializing",
        "pre_f4_sha256": None,
        "receipt_lineage_sha256": "f" * 64,
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "attempt_id": ledger["attempt_id"],
        "terminal_publisher_claim_id": SECOND_CLAIM_ID,
        "terminal_publisher_post_release_ledger_sha256": None,
        "terminal_publisher_post_reservation_ledger_sha256": raw_sha256(
            post_reservation
        ),
        "terminal_publisher_pre_release_ledger_sha256": None,
        "terminal_publisher_pre_reservation_ledger_sha256": raw_sha256(pre_reservation),
        "terminal_publisher_release_authorizations_sha256": None,
        "terminal_publisher_release_basis_sha256": None,
        "terminal_publisher_release_boot_id": None,
        "terminal_publisher_release_context": None,
        "terminal_publisher_release_kind": None,
        "terminal_publisher_released_at_utc": None,
        "terminal_publisher_reservation_at_utc": claim["reserved_at_utc"],
        "terminal_publisher_reservation_spec_sha256": raw_sha256(rfc8785.dumps(spec)),
        "terminal_publisher_state": claim["status"],
        "updated_at_utc": utc_now(),
    }


def _compact_identity(path: StrPath) -> JsonObject:
    identity = stat_identity(Path(path))
    identity.pop("link_count")
    return identity


def _write_failure_receipt(ledger: JsonObject) -> None:
    receipt: JsonObject = {
        "attempt_id": ledger["attempt_id"],
        "cause_code": "boot-changed",
        "cleanup_verified": True,
        "control_tree_sha256": None,
        "ended_at_utc": "2026-07-16T22:00:01.000002Z",
        "exit_code": None,
        "failure_class": "infrastructure",
        "journal_sha256": "d" * 64,
        "lane": "F1",
        "lineage_validation_sha256": "e" * 64,
        "observed_causes": ["boot-changed"],
        "output_sha256": None,
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "signal": None,
        "stage": "F1-review",
        "started_at_utc": "2026-07-16T22:00:00.000001Z",
        "timed_out": False,
    }
    root = Path(str(ledger["attempt_root"])) / "final-failure-receipts"
    write_no_replace(root / "F1.json", canonical_bytes(receipt), mode=MODE_IMMUTABLE)


publisher_claim = _publisher_claim


def terminal_authorizations(root: Path) -> list[JsonValue]:
    values: list[JsonValue] = []
    values.extend(_terminal_authorizations(root))
    return values
