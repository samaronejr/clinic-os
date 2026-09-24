from __future__ import annotations

import importlib
from copy import deepcopy
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from ops.testing.isolation_common import (
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
)
from ops.testing.isolation_ledger_store import BOOT_ID_PATH, LedgerSession

from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_spec,
)

if TYPE_CHECKING:
    from pathlib import Path

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"


class ResumeCoordinator(Protocol):
    def reconcile_stale_boot(
        self,
        ledger_path: Path,
        *,
        inventory_reader: object | None = None,
        checkpoint: object | None = None,
        proof_publisher: object | None = None,
    ) -> JsonObject: ...


def _coordinator() -> ResumeCoordinator:
    return cast(
        "ResumeCoordinator",
        importlib.import_module("ops.testing.isolation_stale_recovery"),
    )


def _empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


def _stale_reserved_ledger(tmp_path: Path) -> Path:
    ledger_path = snapshot(tmp_path)
    claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(CLAIM_ID, [])),
    )
    ledger, _ = load_json(ledger_path)
    ledger["boot_id"] = PREVIOUS_BOOT
    observation = ledger["boot_observation"]
    assert isinstance(observation, dict)
    observation["boot_id"] = PREVIOUS_BOOT
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    return ledger_path


def _reuse_current_proof(
    session: LedgerSession,
    _journal: JsonObject,
) -> JsonObject:
    proof = session.ledger["execution_host_preflight"]
    assert isinstance(proof, dict)
    assert proof["boot_id"] == BOOT_ID_PATH.read_text().strip()
    return deepcopy(proof)


def test_resume_binds_proof_and_post_update_hash_before_boot_rebind(
    tmp_path: Path,
) -> None:
    # Given: one quiescent stale attempt with a current-boot proof publisher.
    ledger_path = _stale_reserved_ledger(tmp_path)
    bound: list[tuple[JsonObject, bytes]] = []

    def observe_binding(stage: str, journal: JsonObject) -> None:
        if stage == "resume-proof-published":
            bound.append((deepcopy(journal), ledger_path.read_bytes()))

    # When: the resume branch completes its proof and boot rebind sequence.
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        checkpoint=observe_binding,
        proof_publisher=_reuse_current_proof,
    )

    # Then: journal authority precedes the sole post-update ledger replacement.
    ledger, ledger_raw = load_json(ledger_path)
    assert len(bound) == 1
    bound_journal, bound_ledger = bound[0]
    assert load_json(ledger_path)[0]["boot_id"] == BOOT_ID_PATH.read_text().strip()
    assert raw_sha256(bound_ledger) == bound_journal["post_cleanup_ledger_sha256"]
    assert bound_journal["post_update_ledger_sha256"] == raw_sha256(ledger_raw)
    assert bound_journal["resume_execution_host_preflight_sha256"] is not None
    assert completed["state"] == "complete"
    assert ledger["claims"] == []
    assert ledger["boot_observation"] == completed["boot_observation"]


@pytest.mark.parametrize(
    "crash_stage",
    ["resume-proof-published", "resume-ledger-updated", "resume-boot-updated"],
)
def test_resume_replays_each_boot_update_crash_prefix(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: one selected durable crash boundary in the resume sequence.
    ledger_path = _stale_reserved_ledger(tmp_path)

    def crash(stage: str, _journal: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated crash at {stage}"
            raise RuntimeError(message)

    # When: the first invocation crashes and the exact command is replayed.
    with pytest.raises(RuntimeError, match="simulated crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash,
            proof_publisher=_reuse_current_proof,
        )
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        proof_publisher=_reuse_current_proof,
    )

    # Then: replay converges on one same-boot ledger and complete journal.
    ledger, ledger_raw = load_json(ledger_path)
    assert completed["state"] == "complete"
    assert ledger["boot_id"] == BOOT_ID_PATH.read_text().strip()
    assert raw_sha256(ledger_raw) == completed["post_update_ledger_sha256"]
