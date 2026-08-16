from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_accepted_close_records import receipt_from_state
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    write_atomic_replace,
    write_no_replace,
)

from isolation_rejection_fixtures import empty_inventory
from isolation_user_fixtures import UserGateFixture, user_gate_fixture

if TYPE_CHECKING:
    from collections.abc import Callable


FINAL_RECEIPT_NAME = "isolation-ledger-final-phase1a.json"


def _crash_close(fixture: UserGateFixture, stage: str) -> tuple[Path, Path]:
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    def stop(observed: str, _state: JsonObject) -> None:
        if observed == stage:
            message = f"stop accepted close at {stage}"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match=f"close at {stage}"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
            checkpoint=stop,
        )
    ledger, _ = load_json(fixture.ledger_path)
    state_path = Path(str(ledger["attempt_root"])) / "accepted-close-state.json"
    return receipt_path, state_path


def test_closed_prefix_finishes_without_terminal_inventory_or_ledger_write(
    tmp_path: Path,
) -> None:
    # Given: accepted close crashed after binding and closing the canonical ledger.
    fixture = user_gate_fixture(tmp_path)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    def stop_after_close(stage: str, _state: JsonObject) -> None:
        if stage == "ledger-closed":
            message = "stop after accepted ledger close"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="stop after accepted ledger close"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
            checkpoint=stop_after_close,
        )
    closed_before = fixture.ledger_path.read_bytes()
    fixture.terminal_path.unlink()
    inventory_reads = 0

    def forbidden_inventory() -> JsonObject:
        nonlocal inventory_reads
        inventory_reads += 1
        return empty_inventory()

    # When: the exact receipt-only form replays from the closed hash-bound prefix.
    result = close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        inventory_reader=forbidden_inventory,
    )

    # Then: only journal/receipt completion occurs; no terminal or ledger is needed.
    closed, _ = load_json(fixture.ledger_path)
    state, _ = load_json(
        Path(str(closed["attempt_root"])) / "accepted-close-state.json"
    )
    assert result == receipt_path
    assert fixture.ledger_path.read_bytes() == closed_before
    assert closed["state"] == "closed"
    assert state["phase"] == "complete"
    assert inventory_reads == 0


def test_prepared_plus_closed_prefix_replays_without_a_terminal_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the ledger replacement persisted before the prepared phase advanced.
    fixture = user_gate_fixture(tmp_path)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)
    module = importlib.import_module("ops.testing.isolation_accepted_close")
    advance = module._advance

    def stop_before_advance(
        _path: Path,
        _state: JsonObject,
        _phase: str,
        _emit: Callable[[str, JsonObject], None],
    ) -> JsonObject:
        message = "stop before accepted phase advance"
        raise RuntimeError(message)

    monkeypatch.setattr(module, "_advance", stop_before_advance)
    with pytest.raises(RuntimeError, match="before accepted phase"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    monkeypatch.setattr(module, "_advance", advance)
    closed_before = fixture.ledger_path.read_bytes()
    fixture.terminal_path.unlink()

    # When: receipt-only recovery classifies prepared plus the exact closed hash.
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        inventory_reader=lambda: pytest.fail("receipt-only read inventory"),
    )

    # Then: it advances from the closed authority without reopening or rebinding.
    closed, _ = load_json(fixture.ledger_path)
    state, _ = load_json(
        Path(str(closed["attempt_root"])) / "accepted-close-state.json"
    )
    assert fixture.ledger_path.read_bytes() == closed_before
    assert closed["state"] == "closed"
    assert state["phase"] == "complete"


@pytest.mark.parametrize(
    "illegal_prefix",
    ["ledger-closed", "receipt-published", "complete", "receipt-with-open-ledger"],
)
def test_open_ledger_rejects_later_phase_or_receipt_prefixes(
    tmp_path: Path,
    illegal_prefix: str,
) -> None:
    # Given: explicit acceptance is prepared but its ledger remains open.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, state_path = _crash_close(fixture, "prepared")
    state, _ = load_json(state_path)
    if illegal_prefix == "receipt-with-open-ledger":
        _receipt, raw = receipt_from_state(state)
        write_no_replace(receipt_path, raw, mode=MODE_IMMUTABLE)
    else:
        state["phase"] = illegal_prefix
        write_atomic_replace(state_path, canonical_bytes(state))
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: classification fails without closing or replacing the open ledger.
    with pytest.raises(IsolationError, match=r"open ledger|phase|receipt"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


def test_closed_ledger_without_its_prepared_journal_is_rejected(
    tmp_path: Path,
) -> None:
    # Given: a closed canonical ledger whose accepted-close journal disappeared.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, state_path = _crash_close(fixture, "ledger-closed")
    state_path.unlink()
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: no receipt can be reconstructed from an unbound closed ledger.
    with pytest.raises(IsolationError, match=r"open-ledger prefix"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


def test_closed_prefix_rejects_ledger_hash_drift(
    tmp_path: Path,
) -> None:
    # Given: a closed ledger differs from the exact bytes bound by prepared state.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, _state_path = _crash_close(fixture, "ledger-closed")
    closed, _ = load_json(fixture.ledger_path)
    closed["last_verified_at_utc"] = closed["closed_at_utc"]
    write_atomic_replace(fixture.ledger_path, canonical_bytes(closed))
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: receipt-only recovery cannot bless the substituted closed bytes.
    with pytest.raises(IsolationError, match=r"closed ledger|target bytes"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


def test_closed_prefix_rejects_a_mismatched_existing_receipt(
    tmp_path: Path,
) -> None:
    # Given: the correct closed prefix beside an unauthorized immutable receipt.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, _state_path = _crash_close(fixture, "ledger-closed")
    write_no_replace(receipt_path, b"{}\n", mode=MODE_IMMUTABLE)
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: replay preserves the ledger and rejects the conflicting receipt.
    with pytest.raises(IsolationError, match=r"receipt"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


def test_explicit_historical_terminal_is_validated_during_receipt_replay(
    tmp_path: Path,
) -> None:
    # Given: a closed hash-bound prefix and an explicitly selected drifted terminal.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, _state_path = _crash_close(fixture, "ledger-closed")
    fixture.terminal_path.chmod(0o600)
    fixture.terminal_path.write_bytes(fixture.terminal_path.read_bytes() + b" ")
    fixture.terminal_path.chmod(0o400)
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: optional historical validation rejects drift without ledger writes.
    with pytest.raises(IsolationError, match=r"historical terminal"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            terminal_revalidation=fixture.terminal_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


def test_closed_prefix_rejects_a_noncanonical_historical_terminal_binding(
    tmp_path: Path,
) -> None:
    # Given: a closed prefix whose journal path is changed outside the attempt root.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, state_path = _crash_close(fixture, "ledger-closed")
    state, _ = load_json(state_path)
    state["terminal_revalidation_relative_path"] = "../../foreign.json"
    write_atomic_replace(state_path, canonical_bytes(state))
    fixture.terminal_path.unlink()
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: receipt-only trust never permits a noncanonical historical path.
    with pytest.raises(IsolationError, match=r"state|terminal.*path"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", "not-a-uuid"),
        ("sha", "A" * 40),
        ("tree_sha", "f" * 39),
        ("closed_at_utc", "2026-07-17T00:00:00Z"),
        ("updated_at_utc", "2000-01-01T00:00:00.000000Z"),
    ],
)
def test_prepared_journal_rejects_malformed_identity_or_timestamps(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    # Given: an open prepared journal with one malformed closed-contract field.
    fixture = user_gate_fixture(tmp_path)
    receipt_path, state_path = _crash_close(fixture, "prepared")
    state, _ = load_json(state_path)
    state[field] = value
    write_atomic_replace(state_path, canonical_bytes(state))
    ledger_before = fixture.ledger_path.read_bytes()

    # When / Then: production validation rejects it before closing the ledger.
    with pytest.raises(IsolationError, match=r"accepted-close"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    assert fixture.ledger_path.read_bytes() == ledger_before
