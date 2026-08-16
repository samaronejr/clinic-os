from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from ops.testing.isolation_common import JsonObject, load_json, raw_sha256

from isolation_terminal_publisher_fixtures import stale_publisher_ledger

if TYPE_CHECKING:
    from pathlib import Path


class PublisherRejectCoordinator(Protocol):
    def reconcile_stale_boot(
        self,
        ledger_path: Path,
        *,
        inventory_reader: object | None = None,
        checkpoint: object | None = None,
        proof_publisher: object | None = None,
    ) -> JsonObject: ...


def _coordinator() -> PublisherRejectCoordinator:
    return cast(
        "PublisherRejectCoordinator",
        importlib.import_module("ops.testing.isolation_stale_recovery"),
    )


def _empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


def _forbid_resume_proof(*_args: object) -> JsonObject:
    message = "resume proof publisher invoked on publisher rejection"
    raise AssertionError(message)


@pytest.mark.parametrize("publisher_status", ["reserved", "active"])
def test_reject_releases_bound_publisher_after_receipt_finalization(
    tmp_path: Path,
    publisher_status: str,
) -> None:
    # Given: one prior-boot persistent publisher and one immutable failure receipt.
    ledger_path, final_wave_path = stale_publisher_ledger(tmp_path, publisher_status)
    stages: list[str] = []

    def observe(stage: str, journal: JsonObject) -> None:
        stages.append(stage)
        if stage in {"receipts-finalized", "publisher-release-intent"}:
            ledger, _ = load_json(ledger_path)
            claims = cast("list[JsonObject]", ledger["claims"])
            assert len(claims) == 1
            assert claims[0]["status"] == publisher_status

    # When: stale rejection completes the journaled publisher release sequence.
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        checkpoint=observe,
        proof_publisher=_forbid_resume_proof,
    )

    # Then: receipt authority and intent precede claim removal and final sealing.
    ledger, ledger_raw = load_json(ledger_path)
    final_wave, final_wave_raw = load_json(final_wave_path)
    assert stages.index("receipts-finalized") < stages.index("publisher-release-intent")
    assert stages.index("publisher-release-intent") < stages.index(
        "reject-ledger-updated"
    )
    assert completed["state"] == "complete"
    assert completed["publisher_release_intent_sha256"] is not None
    assert completed["publisher_final_journal_sha256"] == raw_sha256(final_wave_raw)
    assert final_wave["terminal_publisher_state"] == "released"
    assert final_wave["terminal_publisher_release_kind"] == "rejection-prefix"
    assert final_wave["terminal_publisher_release_context"] == "stale-boot"
    assert (
        final_wave["terminal_publisher_release_basis_sha256"]
        == completed["failure_receipts_sha256"]
    )
    assert final_wave["terminal_publisher_post_release_ledger_sha256"] == raw_sha256(
        ledger_raw
    )
    assert ledger["claims"] == []


@pytest.mark.parametrize("publisher_status", ["reserved", "active"])
@pytest.mark.parametrize(
    "crash_stage",
    [
        "publisher-release-intent-applied",
        "publisher-release-intent",
        "reject-ledger-updated",
        "reject-boot-updated",
        "publisher-released",
    ],
)
def test_reject_replays_each_publisher_release_crash_prefix(
    tmp_path: Path,
    publisher_status: str,
    crash_stage: str,
) -> None:
    # Given: one reserved or active publisher and a selected release crash boundary.
    ledger_path, final_wave_path = stale_publisher_ledger(tmp_path, publisher_status)

    def crash(stage: str, _journal: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated crash at {stage}"
            raise RuntimeError(message)

    # When: the first invocation crashes and exact stale recovery is replayed.
    with pytest.raises(RuntimeError, match="simulated crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash,
            proof_publisher=_forbid_resume_proof,
        )
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        proof_publisher=_forbid_resume_proof,
    )

    # Then: replay converges on one released journal and its exact bound ledger.
    ledger, ledger_raw = load_json(ledger_path)
    final_wave, final_wave_raw = load_json(final_wave_path)
    assert completed["state"] == "complete"
    assert final_wave["terminal_publisher_state"] == "released"
    assert completed["publisher_final_journal_sha256"] == raw_sha256(final_wave_raw)
    assert raw_sha256(ledger_raw) == completed["post_update_ledger_sha256"]
    assert ledger["claims"] == []
