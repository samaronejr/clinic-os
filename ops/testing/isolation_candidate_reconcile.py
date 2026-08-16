"""Finish a bound candidate's same-boot staging and history release."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_candidate_contract import contract_for_purpose
from ops.testing.isolation_candidate_records import (
    bound_envelope_bytes,
    candidate_observations,
    object_value,
    object_values,
    published_observation,
    text_value,
)
from ops.testing.isolation_candidate_release import release_candidate_claim
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_stale_filesystem_execution import remove_stale_staging

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession


def reconcile_bound_candidate(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
) -> None:
    """Remove exact binding-derived staging before the ordinary history routine."""
    contract = contract_for_purpose(claim.get("purpose"))
    if contract is None or claim.get("status") != "active":
        _fail("candidate reconcile source is not an active publisher")
    binding = object_value(
        claim.get("candidate_envelope_binding"), "candidate envelope binding"
    )
    _, raw = bound_envelope_bytes(binding, session.ledger, claim, contract)
    desired = object_value(claim.get("desired"), "candidate desired")
    authorizations = object_values(
        desired.get("published_outputs"), "candidate authorizations"
    )
    observations = candidate_observations(claim, authorizations)
    if observations[0] != published_observation(authorizations[0], raw):
        _fail("candidate envelope is not durably published before reconcile")
    attempt_root = Path(text_value(session.ledger.get("attempt_root"), "attempt root"))
    claim_root = attempt_root / text_value(
        claim.get("root_relative_path"), "claim root"
    )
    remove_stale_staging(
        {
            "claim_root_path": str(claim_root),
            "expected_staged_entry": {
                "gid": authorizations[0]["gid"],
                "mode": 0o400,
                "relative_path": "candidate-envelope.json",
                "sha256": binding["envelope_sha256"],
                "size_bytes": len(raw),
                "uid": authorizations[0]["uid"],
            },
        }
    )
    release_candidate_claim(session, claims, claim, claim_root)


def _fail(message: str) -> Never:
    raise IsolationError(message)
