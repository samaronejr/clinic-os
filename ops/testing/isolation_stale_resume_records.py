"""Build the write-ahead resume proof and boot-rebind records."""

from __future__ import annotations

import copy
from typing import Final, Never

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    raw_sha256,
    utc_now,
)

SHA256_LENGTH: Final = 64


def build_resumed_ledger(
    ledger: JsonObject,
    journal: JsonObject,
    proof: JsonObject,
) -> tuple[JsonObject, bytes]:
    """Construct the sole current-boot ledger bytes authorized by resume."""
    if journal.get("recovery_goal") != "resume" or journal.get("state") not in {
        "claims-pruned",
        "resume-proof-published",
    }:
        _fail("resume ledger construction lacks claims-pruned authority")
    current_boot = journal.get("current_boot_id")
    if proof.get("boot_id") != current_boot:
        _fail("resume proof belongs to a different boot")
    path = proof.get("path")
    digest = proof.get("sha256")
    if not isinstance(path, str) or not path.startswith("/"):
        _fail("resume proof path is not absolute")
    if not isinstance(digest, str) or len(digest) != SHA256_LENGTH:
        _fail("resume proof hash is invalid")
    observation = journal.get("boot_observation")
    if not isinstance(observation, dict):
        _fail("resume boot observation is not an object")
    timestamp = observation.get("observed_at_utc")
    if not isinstance(timestamp, str):
        _fail("resume boot observation timestamp is invalid")
    result = copy.deepcopy(ledger)
    result["boot_id"] = current_boot
    result["boot_observation"] = copy.deepcopy(observation)
    result["execution_host_preflight"] = copy.deepcopy(proof)
    result["last_verified_at_utc"] = timestamp
    return result, canonical_bytes(result)


def bind_resume_proof(
    journal: JsonObject,
    proof: JsonObject,
    updated_raw: bytes,
) -> JsonObject:
    """Bind proof and prospective ledger bytes before the boot rebind."""
    if journal.get("state") != "claims-pruned":
        _fail("resume proof binding requires claims-pruned")
    if journal.get("recovery_goal") != "resume":
        _fail("reject recovery cannot bind a resume proof")
    result = copy.deepcopy(journal)
    result["resume_execution_host_preflight_path"] = proof.get("path")
    result["resume_execution_host_preflight_sha256"] = proof.get("sha256")
    result["post_update_ledger_sha256"] = raw_sha256(updated_raw)
    if not all(
        isinstance(result.get(key), str)
        for key in (
            "resume_execution_host_preflight_path",
            "resume_execution_host_preflight_sha256",
            "post_update_ledger_sha256",
        )
    ):
        _fail("resume proof binding is incomplete")
    result["state"] = "resume-proof-published"
    result["updated_at_utc"] = utc_now()
    return result


def mark_resume_boot_updated(journal: JsonObject, ledger_raw: bytes) -> JsonObject:
    """Acknowledge only ledger bytes prebound beside the published proof."""
    if journal.get("state") != "resume-proof-published":
        _fail("resume journal is not proof-published")
    if raw_sha256(ledger_raw) != journal.get("post_update_ledger_sha256"):
        _fail("resume ledger differs from its write-ahead hash")
    result = copy.deepcopy(journal)
    result["state"] = "boot-updated"
    result["updated_at_utc"] = utc_now()
    return result


def mark_resume_complete(journal: JsonObject, ledger_raw: bytes) -> JsonObject:
    """Complete resume only after the current-boot ledger remains bound."""
    if journal.get("state") != "boot-updated":
        _fail("resume journal has not acknowledged the boot update")
    if raw_sha256(ledger_raw) != journal.get("post_update_ledger_sha256"):
        _fail("completed resume ledger differs from write-ahead authority")
    result = copy.deepcopy(journal)
    result["state"] = "complete"
    result["updated_at_utc"] = utc_now()
    return result


def _fail(message: str) -> Never:
    raise IsolationError(message)
