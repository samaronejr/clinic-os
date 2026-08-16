"""Build write-ahead records for receipt-driven stale-boot rejection."""

from __future__ import annotations

import copy
from typing import Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    raw_sha256,
    utc_now,
)


def mark_receipts_finalizing(journal: JsonObject) -> JsonObject:
    """Enter immutable receipt finalization before any rejection boot update."""
    if journal.get("recovery_goal") != "reject":
        _fail("resume recovery cannot finalize rejection receipts")
    if journal.get("state") != "claims-pruned":
        _fail("rejection receipts require claims-pruned authority")
    if any(
        journal.get(key) is not None
        for key in (
            "failure_receipts_sha256",
            "post_update_ledger_sha256",
            "resume_execution_host_preflight_path",
            "resume_execution_host_preflight_sha256",
        )
    ):
        _fail("rejection receipt finalization fields were bound early")
    result = copy.deepcopy(journal)
    result["state"] = "receipts-finalizing"
    result["updated_at_utc"] = utc_now()
    return result


def build_rejected_ledger(
    ledger: JsonObject,
    journal: JsonObject,
) -> tuple[JsonObject, bytes]:
    """Construct the current-boot claim-free ledger authorized by rejection."""
    if journal.get("recovery_goal") != "reject" or journal.get("state") not in {
        "receipts-finalizing",
        "receipts-finalized",
        "publisher-release-intent",
    }:
        _fail("rejected ledger construction lacks finalized-receipt authority")
    claims = ledger.get("claims")
    if not isinstance(claims, list) or not all(
        isinstance(item, dict) for item in claims
    ):
        _fail("rejection ledger claims are not an object array")
    claim_objects = cast("list[JsonObject]", claims)
    publisher_id = journal.get("publisher_claim_id")
    if publisher_id is None and claim_objects != []:
        _fail("receipt-only rejection retained a claim")
    if publisher_id is not None and claim_objects not in (
        [],
        [
            item
            for item in claim_objects
            if item.get("claim_id") == publisher_id
            and item.get("purpose") == "final-terminal-publisher"
        ],
    ):
        _fail("publisher rejection retained a foreign claim")
    if publisher_id is not None and len(claim_objects) > 1:
        _fail("publisher rejection retained more than its bound claim")
    observation = journal.get("boot_observation")
    if not isinstance(observation, dict):
        _fail("rejection boot observation is not an object")
    timestamp = observation.get("observed_at_utc")
    if not isinstance(timestamp, str):
        _fail("rejection boot observation timestamp is invalid")
    result = copy.deepcopy(ledger)
    result["boot_id"] = journal.get("current_boot_id")
    result["boot_observation"] = copy.deepcopy(observation)
    result["last_verified_at_utc"] = timestamp
    result["claims"] = []
    return result, canonical_bytes(result)


def bind_finalized_receipts(
    journal: JsonObject,
    aggregate_sha256: str,
    updated_raw: bytes,
) -> JsonObject:
    """Bind the receipt aggregate and prospective ledger before replacement."""
    if journal.get("state") != "receipts-finalizing":
        _fail("failure receipts are not finalizing")
    if not aggregate_sha256:
        _fail("rejection requires at least one failure receipt")
    result = copy.deepcopy(journal)
    result["failure_receipts_sha256"] = aggregate_sha256
    result["post_update_ledger_sha256"] = raw_sha256(updated_raw)
    result["state"] = "receipts-finalized"
    result["updated_at_utc"] = utc_now()
    return result


def mark_reject_boot_updated(journal: JsonObject, ledger_raw: bytes) -> JsonObject:
    """Acknowledge only the exact receipt-bound rejection ledger bytes."""
    publisher = journal.get("publisher_claim_id") is not None
    expected = "publisher-release-intent" if publisher else "receipts-finalized"
    if journal.get("state") != expected:
        _fail("rejection journal lacks its required pre-update authority")
    if raw_sha256(ledger_raw) != journal.get("post_update_ledger_sha256"):
        _fail("rejection ledger differs from its write-ahead hash")
    result = copy.deepcopy(journal)
    result["state"] = "boot-updated"
    result["updated_at_utc"] = utc_now()
    return result


def mark_reject_complete(journal: JsonObject, ledger_raw: bytes) -> JsonObject:
    """Complete receipt-only rejection after the boot update remains bound."""
    publisher = journal.get("publisher_claim_id") is not None
    expected = "publisher-released" if publisher else "boot-updated"
    if journal.get("state") != expected:
        _fail("rejection boot update or publisher release is not acknowledged")
    if raw_sha256(ledger_raw) != journal.get("post_update_ledger_sha256"):
        _fail("completed rejection differs from write-ahead authority")
    result = copy.deepcopy(journal)
    result["state"] = "complete"
    result["updated_at_utc"] = utc_now()
    return result


def bind_publisher_release_intent(
    journal: JsonObject,
    intent_sha256: str,
) -> JsonObject:
    """Bind the inner publisher intent before the canonical ledger update."""
    if journal.get("state") != "receipts-finalized":
        _fail("publisher intent requires receipts-finalized")
    if journal.get("publisher_claim_id") is None:
        _fail("receipt-only rejection cannot bind publisher intent")
    if journal.get("publisher_release_intent_sha256") is not None:
        _fail("publisher release intent hash is already bound")
    result = copy.deepcopy(journal)
    result["publisher_release_intent_sha256"] = intent_sha256
    result["state"] = "publisher-release-intent"
    result["updated_at_utc"] = utc_now()
    return result


def bind_publisher_released(
    journal: JsonObject,
    final_sha256: str,
) -> JsonObject:
    """Bind the terminal released journal after the claim-free boot update."""
    if journal.get("state") != "boot-updated":
        _fail("publisher release completion requires boot-updated")
    if journal.get("publisher_claim_id") is None:
        _fail("receipt-only rejection cannot bind a publisher final journal")
    if journal.get("publisher_release_intent_sha256") is None:
        _fail("publisher release completion lacks its intent hash")
    result = copy.deepcopy(journal)
    result["publisher_final_journal_sha256"] = final_sha256
    result["state"] = "publisher-released"
    result["updated_at_utc"] = utc_now()
    return result


def _fail(message: str) -> Never:
    raise IsolationError(message)
