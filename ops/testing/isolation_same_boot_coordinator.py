"""Coordinate runner recovery before ordinary same-boot reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_reconcile import reconcile_same_boot as reconcile_claims
from ops.testing.isolation_runner_lifecycle import discard_runner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject
    from ops.testing.isolation_docker_metadata import CommandRunner
    from ops.testing.isolation_live_claims import ClaimObserver


def reconcile_same_boot(
    ledger_path: Path,
    *,
    docker_runner: CommandRunner | None = None,
    inventory_reader: Callable[[], JsonObject] | None = None,
    claim_observer: ClaimObserver | None = None,
) -> None:
    """Finish runner tombstones before the ordinary same-boot pass."""
    for claim_id, reason in _runner_cleanup_requests(ledger_path):
        discard_runner(ledger_path, claim_id, reason, docker_runner)
    reconcile_claims(
        ledger_path,
        inventory_reader=inventory_reader,
        claim_observer=claim_observer,
    )


def _runner_cleanup_requests(ledger_path: Path) -> list[tuple[str, str]]:
    with locked_open_ledger(ledger_path) as session:
        requests: list[tuple[str, str]] = []
        for claim in claim_objects(session.ledger["claims"]):
            creation = claim.get("runner_creation")
            if not isinstance(creation, dict):
                continue
            state = creation.get("state")
            if state == "intent":
                requests.append((str(claim["claim_id"]), "owner-lost"))
            elif state in {"remove-intent", "removed"}:
                requests.append(
                    (str(claim["claim_id"]), str(creation["remove_reason"]))
                )
        return requests
