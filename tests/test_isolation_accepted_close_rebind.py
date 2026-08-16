from __future__ import annotations

import importlib
from pathlib import Path
from typing import Final

import pytest
from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
)
from ops.testing.isolation_terminal_reconcile import reconcile_terminal_final

from isolation_claim_fixtures import FOUNDATION_SHA
from isolation_rejection_fixtures import empty_inventory
from isolation_user_fixtures import UserGateFixture, user_gate_fixture

FINAL_RECEIPT_NAME: Final = "isolation-ledger-final-phase1a.json"
FIRST_REBOOT: Final = "11111111-1111-4111-8111-111111111111"
SECOND_REBOOT: Final = "22222222-2222-4222-8222-222222222222"


def _prepare_acceptance(fixture: UserGateFixture, receipt_path: Path) -> Path:
    def stop_prepared(stage: str, _state: JsonObject) -> None:
        if stage == "prepared":
            message = "stop at prepared acceptance"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="prepared acceptance"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
            checkpoint=stop_prepared,
        )
    ledger, _ = load_json(fixture.ledger_path)
    return Path(str(ledger["attempt_root"])) / "accepted-close-state.json"


def _boot_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "accepted-close-current-boot"
    for module_name in (
        "ops.testing.isolation_ledger_store",
        "ops.testing.isolation_terminal_reconcile",
        "ops.testing.isolation_terminal_revalidation",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "BOOT_ID_PATH", path)
    return path


def _reconcile(fixture: UserGateFixture) -> Path:
    return reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
    )


def _stop_rebound(stage: str, _state: JsonObject) -> None:
    if stage == "prepared":
        message = "stop after prepared rebind"
        raise RuntimeError(message)


def test_two_reboots_bind_each_immediate_prepared_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one explicit acceptance stopped at its initial prepared generation.
    fixture = user_gate_fixture(tmp_path)
    receipt = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)
    state_path = _prepare_acceptance(fixture, receipt)
    initial_sha = raw_sha256(state_path.read_bytes())
    boot_path = _boot_control(tmp_path, monkeypatch)

    # When: two successive reboots reconcile and rebind before final close.
    boot_path.write_text(f"{FIRST_REBOOT}\n")
    first_terminal = _reconcile(fixture)
    first_artifact, _ = load_json(first_terminal)
    with pytest.raises(RuntimeError, match="prepared rebind"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt,
            terminal_revalidation=first_terminal,
            inventory_reader=empty_inventory,
            checkpoint=_stop_rebound,
        )
    first_rebound_sha = raw_sha256(state_path.read_bytes())
    boot_path.write_text(f"{SECOND_REBOOT}\n")
    second_terminal = _reconcile(fixture)
    second_artifact, _ = load_json(second_terminal)
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt,
        terminal_revalidation=second_terminal,
        inventory_reader=empty_inventory,
    )

    # Then: each artifact names only the exact prepared generation it supersedes.
    closed, _ = load_json(fixture.ledger_path)
    assert first_artifact["accepted_close_prepared_sha256"] == initial_sha
    assert second_artifact["accepted_close_prepared_sha256"] == first_rebound_sha
    assert first_rebound_sha != initial_sha
    assert closed["state"] == "closed"


def test_rebind_rejects_a_gapped_prepared_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a second reboot artifact is changed to skip its immediate predecessor.
    fixture = user_gate_fixture(tmp_path)
    receipt = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)
    state_path = _prepare_acceptance(fixture, receipt)
    initial_sha = raw_sha256(state_path.read_bytes())
    boot_path = _boot_control(tmp_path, monkeypatch)
    boot_path.write_text(f"{FIRST_REBOOT}\n")
    first_terminal = _reconcile(fixture)
    with pytest.raises(RuntimeError, match="prepared rebind"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt,
            terminal_revalidation=first_terminal,
            inventory_reader=empty_inventory,
            checkpoint=_stop_rebound,
        )
    immediate_sha = raw_sha256(state_path.read_bytes())
    boot_path.write_text(f"{SECOND_REBOOT}\n")
    second_terminal = _reconcile(fixture)
    artifact, _ = load_json(second_terminal)
    artifact["accepted_close_prepared_sha256"] = initial_sha
    second_terminal.chmod(0o600)
    second_terminal.write_bytes(canonical_bytes(artifact))
    second_terminal.chmod(0o400)

    # When / Then: close rejects the gap without changing prepared or open-ledger bytes.
    ledger_before = fixture.ledger_path.read_bytes()
    with pytest.raises(IsolationError, match=r"prepared|terminal|predecessor"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt,
            terminal_revalidation=second_terminal,
            inventory_reader=empty_inventory,
        )
    assert raw_sha256(state_path.read_bytes()) == immediate_sha
    assert fixture.ledger_path.read_bytes() == ledger_before


@pytest.mark.parametrize("crash_side", ["before", "after"])
def test_prepared_rebind_recovers_atomic_replace_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_side: str,
) -> None:
    # Given: current terminal authority for one prepared-to-prepared replacement.
    fixture = user_gate_fixture(tmp_path)
    receipt = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)
    state_path = _prepare_acceptance(fixture, receipt)
    previous = state_path.read_bytes()
    boot_path = _boot_control(tmp_path, monkeypatch)
    boot_path.write_text(f"{FIRST_REBOOT}\n")
    terminal = _reconcile(fixture)
    module = importlib.import_module("ops.testing.isolation_accepted_close_recovery")
    replace = module.write_atomic_replace

    def crash(path: Path, raw: bytes) -> None:
        if crash_side == "after":
            replace(path, raw)
        message = f"rebind {crash_side} replace crash"
        raise RuntimeError(message)

    monkeypatch.setattr(module, "write_atomic_replace", crash)

    # When: replacement loses either immediately before or after its durable write.
    with pytest.raises(RuntimeError, match=f"rebind {crash_side}"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt,
            terminal_revalidation=terminal,
            inventory_reader=empty_inventory,
        )
    observed = state_path.read_bytes()
    monkeypatch.setattr(module, "write_atomic_replace", replace)
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt,
        terminal_revalidation=terminal,
        inventory_reader=empty_inventory,
    )

    # Then: old-or-new durable state replays to the same closed accepted outcome.
    ledger, _ = load_json(fixture.ledger_path)
    assert (observed == previous) is (crash_side == "before")
    assert ledger["state"] == "closed"
    assert receipt.exists()
