from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path
from typing import Protocol, cast

import pytest
from ops.testing.isolation_common import (
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
)
from ops.testing.isolation_ledger import run_cli
from ops.testing.isolation_ledger_store import BOOT_ID_PATH, LedgerSession

from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_spec,
)

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"


class StaleCoordinator(Protocol):
    def reconcile_stale_boot(
        self,
        ledger_path: Path,
        *,
        inventory_reader: object | None = None,
        checkpoint: object | None = None,
        proof_publisher: object | None = None,
    ) -> JsonObject: ...


def _coordinator() -> StaleCoordinator:
    try:
        module = importlib.import_module("ops.testing.isolation_stale_recovery")
    except ModuleNotFoundError:
        pytest.fail("stale-boot executable coordinator is missing")
    return cast("StaleCoordinator", module)


def _empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


def _reuse_current_proof(
    session: LedgerSession,
    _journal: JsonObject,
) -> JsonObject:
    proof = session.ledger["execution_host_preflight"]
    assert isinstance(proof, dict)
    return deepcopy(proof)


def _stale_reserved_ledger(tmp_path: Path) -> tuple[Path, bytes]:
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
    return ledger_path, ledger_path.read_bytes()


def test_stale_coordinator_keeps_prior_ledger_through_physical_replay(
    tmp_path: Path,
) -> None:
    # Given: a prior-boot reserved root and a crash after physical removal.
    ledger_path, prior_raw = _stale_reserved_ledger(tmp_path)

    def crash_after_action(stage: str, _journal: JsonObject) -> None:
        if stage == "resource-action-applied":
            assert ledger_path.read_bytes() == prior_raw
            message = "simulated crash after physical action"
            raise RuntimeError(message)

    # When: recovery crashes, then replays the already-absent staging root.
    with pytest.raises(RuntimeError, match="simulated crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash_after_action,
            proof_publisher=_reuse_current_proof,
        )
    journal_path = Path(str(load_json(ledger_path)[0]["attempt_root"]))
    journal_path /= "stale-boot-recovery.json"
    prepared, _ = load_json(journal_path)
    assert prepared["state"] == "prepared"
    assert ledger_path.read_bytes() == prior_raw
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        proof_publisher=_reuse_current_proof,
    )

    # Then: replay prunes first and only afterward rebinds current-boot proof.
    ledger, ledger_raw = load_json(ledger_path)
    assert completed["state"] == "complete"
    assert completed["completed_action_ids"] == [f"staging-remove-{CLAIM_ID}"]
    assert completed["prior_ledger_sha256"] == raw_sha256(prior_raw)
    assert completed["post_update_ledger_sha256"] == raw_sha256(ledger_raw)
    assert ledger["boot_id"] == BOOT_ID_PATH.read_text().strip()
    assert ledger["claims"] == []


@pytest.mark.parametrize("crash_stage", ["claims-prune-intent", "ledger-pruned"])
def test_stale_coordinator_recovers_each_write_ahead_prune_prefix(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: a stale claim and one selected write-ahead crash boundary.
    ledger_path, prior_raw = _stale_reserved_ledger(tmp_path)

    def crash_at_prune(stage: str, journal: JsonObject) -> None:
        if stage != crash_stage:
            return
        if stage == "claims-prune-intent":
            assert ledger_path.read_bytes() == prior_raw
        else:
            assert (
                raw_sha256(ledger_path.read_bytes())
                == journal["post_cleanup_ledger_sha256"]
            )
        message = f"simulated crash at {stage}"
        raise RuntimeError(message)

    # When: one prefix crashes and the same exact command is replayed.
    with pytest.raises(RuntimeError, match="simulated crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash_at_prune,
            proof_publisher=_reuse_current_proof,
        )
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        proof_publisher=_reuse_current_proof,
    )

    # Then: the journal adopts only prior-or-bound-post ledger bytes.
    assert completed["state"] == "complete"
    assert (
        raw_sha256(ledger_path.read_bytes()) == completed["post_update_ledger_sha256"]
    )


def test_stale_cli_accepts_only_the_exact_two_token_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a feature worktree bound to one changed-boot ledger.
    ledger_path, _ = _stale_reserved_ledger(tmp_path)
    ledger, _ = load_json(ledger_path)
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))
    recovery = importlib.import_module("ops.testing.isolation_stale_recovery")
    monkeypatch.setattr(recovery, "capture_host_inventory", _empty_inventory)
    monkeypatch.setattr(recovery, "publish_resume_proof", _reuse_current_proof)

    # When / Then: the exact form succeeds and reordered/extra forms fail.
    assert run_cli(("reconcile", "--stale-boot")) == 0
    before = ledger_path.read_bytes()
    assert run_cli(("reconcile", "--stale-boot", "extra")) == 2
    assert run_cli(("--stale-boot", "reconcile")) == 2
    assert ledger_path.read_bytes() == before
