"""Authenticate stale-resume proof publication before the host probe writes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

JOURNAL_KEYS: Final = {
    "attempt_id",
    "boot_observation",
    "boot_observation_sha256",
    "completed_action_ids",
    "controller_recoveries",
    "current_boot_id",
    "f3_final_journal_sha256",
    "f3_initial_journal_sha256",
    "f3_journal_path",
    "f3_receipt_path",
    "f3_receipt_sha256",
    "f3_recovery_required",
    "failure_receipts_sha256",
    "lock_identity_sha256",
    "post_cleanup_ledger_sha256",
    "post_update_ledger_sha256",
    "previous_boot_id",
    "prior_ledger_sha256",
    "publisher_claim_id",
    "publisher_final_journal_sha256",
    "publisher_final_wave_journal_path",
    "publisher_initial_journal_sha256",
    "publisher_release_intent_sha256",
    "reboot_stable_baseline_sha256",
    "recovery_goal",
    "resource_actions",
    "resume_execution_host_preflight_path",
    "resume_execution_host_preflight_sha256",
    "runner_recoveries",
    "schema_version",
    "shared_evidence_manifest_sha256",
    "state",
    "updated_at_utc",
}
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
REPLAY_STATES: Final = {"claims-pruned", "resume-proof-published"}


@dataclass(frozen=True, slots=True)
class ResumePublicationInputs:
    """Closed journal, ledger, and path inputs for proof publication."""

    journal: object
    journal_raw: bytes
    journal_path: Path
    ledger: object
    ledger_raw: bytes
    ledger_path: Path
    authority_root: Path
    boot_id: str
    proof_path: Path


def validate_resume_publication_authority(inputs: ResumePublicationInputs) -> None:
    """Require the two exact journal/ledger prefixes allowed to publish proof."""
    journal = inputs.journal
    ledger = inputs.ledger
    if not isinstance(journal, dict) or set(journal) != JOURNAL_KEYS:
        _fail("stale-resume journal has an open or unknown root")
    if not isinstance(ledger, dict) or _canonical(journal) != inputs.journal_raw:
        _fail("stale-resume authority is not canonical JSON")
    if _canonical(ledger) != inputs.ledger_raw:
        _fail("stale-resume ledger is not canonical JSON")
    state = journal.get("state")
    if (
        journal.get("schema_version") != 1
        or journal.get("recovery_goal") != "resume"
        or state not in REPLAY_STATES
        or journal.get("current_boot_id") != inputs.boot_id
        or journal.get("attempt_id") != ledger.get("attempt_id")
    ):
        _fail("stale-resume journal is not proof-publication authority")
    expected_ledger = inputs.authority_root / "evidence/isolation-ledger-phase1a.json"
    attempt_root = _absolute_path(ledger.get("attempt_root"), "attempt root")
    if inputs.ledger_path != expected_ledger or inputs.journal_path != (
        attempt_root / "stale-boot-recovery.json"
    ):
        _fail("stale-resume journal or ledger path is noncanonical")
    _validate_quiescent(journal, ledger)
    _validate_ledger_prefix(
        journal,
        ledger,
        inputs.ledger_raw,
        inputs.boot_id,
        state,
    )
    _validate_proof_binding(
        journal,
        inputs.authority_root,
        inputs.boot_id,
        inputs.proof_path,
        state,
    )


def _validate_quiescent(
    journal: dict[object, object], ledger: dict[object, object]
) -> None:
    if (
        journal.get("controller_recoveries") != []
        or journal.get("f3_recovery_required") is not False
        or journal.get("failure_receipts_sha256") is not None
        or journal.get("publisher_release_intent_sha256") is not None
        or journal.get("publisher_final_journal_sha256") is not None
    ):
        _fail("stale-resume proof publication is not quiescent")
    _validate_completed_resources(journal)
    _validate_retained_claim(journal, ledger)


def _validate_completed_resources(journal: dict[object, object]) -> None:
    actions = journal.get("resource_actions")
    completed = journal.get("completed_action_ids")
    if not isinstance(actions, list) or not all(
        isinstance(item, dict) for item in actions
    ):
        _fail("stale-resume resource actions are malformed")
    action_ids = [item.get("action_id") for item in actions]
    if completed != action_ids:
        _fail("stale-resume physical actions are not complete")
    recoveries = journal.get("runner_recoveries")
    if not isinstance(recoveries, list):
        _fail("stale-resume runner recovery is incomplete")
    for item in recoveries:
        if not isinstance(item, dict):
            _fail("stale-resume runner recovery is incomplete")
        creation = item.get("runner_creation")
        if not isinstance(creation, dict) or creation.get("state") != "removed":
            _fail("stale-resume runner recovery is incomplete")


def _validate_retained_claim(
    journal: dict[object, object], ledger: dict[object, object]
) -> None:
    claims = ledger.get("claims")
    if not isinstance(claims, list) or not all(
        isinstance(item, dict) for item in claims
    ):
        _fail("stale-resume ledger claims are malformed")
    publisher_id = journal.get("publisher_claim_id")
    if claims:
        if (
            len(claims) != 1
            or claims[0].get("purpose") != "final-terminal-publisher"
            or claims[0].get("claim_id") != publisher_id
        ):
            _fail("stale-resume ledger retained a nonpublisher claim")
    elif publisher_id is not None:
        _fail("stale-resume journal lost its retained publisher")


def _validate_ledger_prefix(
    journal: dict[object, object],
    ledger: dict[object, object],
    ledger_raw: bytes,
    boot_id: str,
    state: object,
) -> None:
    cleanup_sha = _sha(journal.get("post_cleanup_ledger_sha256"), "cleanup ledger")
    current_sha = hashlib.sha256(ledger_raw).hexdigest()
    if state == "claims-pruned":
        if current_sha != cleanup_sha or ledger.get("boot_id") != journal.get(
            "previous_boot_id"
        ):
            _fail("claims-pruned ledger differs from stale-resume authority")
        return
    update_sha = _sha(journal.get("post_update_ledger_sha256"), "updated ledger")
    if current_sha not in {cleanup_sha, update_sha}:
        _fail("proof-published ledger is neither its pre nor post image")
    expected_boot = (
        journal.get("previous_boot_id") if current_sha == cleanup_sha else boot_id
    )
    if ledger.get("boot_id") != expected_boot:
        _fail("proof-published ledger boot differs from its hash prefix")


def _validate_proof_binding(
    journal: dict[object, object],
    authority_root: Path,
    boot_id: str,
    proof_path: Path,
    state: object,
) -> None:
    expected = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    if proof_path != expected:
        _fail("stale-resume proof path is noncanonical")
    bound_path = journal.get("resume_execution_host_preflight_path")
    bound_sha = journal.get("resume_execution_host_preflight_sha256")
    update_sha = journal.get("post_update_ledger_sha256")
    if state == "claims-pruned":
        if bound_path is not None or bound_sha is not None or update_sha is not None:
            _fail("claims-pruned journal bound proof fields too early")
        return
    if bound_path != str(expected):
        _fail("replayed stale-resume proof path changed")
    _sha(bound_sha, "resume proof")
    _sha(update_sha, "updated ledger")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _absolute_path(value: object, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is not absolute")
    return Path(value)


def _sha(value: object, context: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        _fail(f"{context} hash is invalid")
    return value


def _fail(message: str) -> Never:
    raise ValueError(message)
