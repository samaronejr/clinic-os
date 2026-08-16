"""Authenticate accepted-close open, rebound, and closed recovery prefixes."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_accepted_close_records import (
    PRESERVED_REBIND_KEYS,
    PreparedAcceptedClose,
    preclose_from_closed,
    prepare_accepted_close,
    receipt_from_state,
    validate_accepted_close_state,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    utc_now,
    write_atomic_replace,
)
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_terminal_records import (
    TerminalValidationInputs,
    ValidatedTerminalRevalidation,
)
from ops.testing.isolation_terminal_revalidation import (
    validate_accepted_terminal_revalidation,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class AcceptedCloseRecoveryInputs:
    """Closed inputs for one journal-authenticated accepted-close replay."""

    ledger_path: Path
    ledger: JsonObject
    ledger_raw: bytes
    state_path: Path
    state: JsonObject
    state_raw: bytes
    receipt_path: Path
    terminal_path: Path | None
    inventory_reader: Callable[[], JsonObject]
    emit: Callable[[str, JsonObject], None]


def recover_prepared_close(
    inputs: AcceptedCloseRecoveryInputs,
) -> PreparedAcceptedClose:
    """Recover or rebind only a hash-complete accepted-close prefix."""
    state = inputs.state
    ledger = inputs.ledger
    validate_accepted_close_state(state)
    if state.get("attempt_id") != ledger.get("attempt_id"):
        _fail("accepted-close state belongs to another attempt")
    if ledger.get("state") == "closed":
        _validate_historical_terminal(ledger, state, inputs.terminal_path)
        preclose, preclose_raw = preclose_from_closed(ledger)
        return _prepared_from_state(preclose, preclose_raw, state)
    if state.get("phase") != "prepared" or _present(inputs.receipt_path):
        _fail("open ledger disagrees with accepted-close phase or receipt")
    terminal = validate_open_close_prefix(
        inputs.ledger_path,
        ledger,
        inputs.ledger_raw,
        inputs.terminal_path,
        inputs.inventory_reader,
    )
    direct = prepare_accepted_close(
        ledger,
        inputs.ledger_raw,
        terminal,
        closed_at_utc=_text(state.get("closed_at_utc"), "close timestamp"),
        updated_at_utc=_text(state.get("updated_at_utc"), "update timestamp"),
    )
    if direct.state == state:
        return direct
    if terminal.artifact.get("accepted_close_prepared_sha256") != raw_sha256(
        inputs.state_raw
    ):
        _fail("terminal artifact does not authorize prepared close rebinding")
    if any(direct.state.get(key) != state.get(key) for key in PRESERVED_REBIND_KEYS):
        _fail("prepared close rebinding changed immutable acceptance scope")
    timestamp = utc_now()
    rebound = prepare_accepted_close(
        ledger,
        inputs.ledger_raw,
        terminal,
        closed_at_utc=timestamp,
        updated_at_utc=timestamp,
    )
    write_atomic_replace(inputs.state_path, canonical_bytes(rebound.state))
    inputs.emit("prepared", rebound.state)
    return rebound


def validate_open_close_prefix(
    ledger_path: Path,
    ledger: JsonObject,
    ledger_raw: bytes,
    terminal_argument: Path | None,
    inventory_reader: Callable[[], JsonObject],
) -> ValidatedTerminalRevalidation:
    """Validate an open ledger against its current terminal artifact."""
    if ledger.get("rejection_close") is not None:
        _fail("rejected ledger cannot enter accepted close")
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    for name in ("rejection-spec.json", "user-fix-intent.json"):
        if _present(attempt_root / name):
            _fail("rejection or USER intent already owns this terminal scope")
    require_quiescent_attempt(ledger_path, ledger, inventory_reader)
    current_boot = _text(ledger.get("boot_id"), "ledger boot ID")
    expected = attempt_root / "terminal-revalidation" / f"{current_boot}.json"
    terminal = validate_accepted_terminal_revalidation(
        TerminalValidationInputs(
            ledger_path,
            ledger,
            ledger_raw,
            _terminal_sha(expected),
            expected,
            ledger_path.parent / "clinic-os-phase1a-final",
        )
    )
    changed = terminal.artifact.get("boot_changed") is True
    if (terminal_argument is not None) != changed:
        _fail("accepted close terminal argument differs from boot relation")
    if terminal_argument is not None and terminal_argument != expected:
        _fail("accepted close terminal argument is noncanonical")
    return terminal


def validate_closed_close_prefix(
    ledger: JsonObject,
    ledger_raw: bytes,
    state: JsonObject,
) -> None:
    """Bind receipt recovery to the exact accepted closed-ledger bytes."""
    if (
        ledger.get("state") != "closed"
        or ledger.get("rejection_close") is not None
        or raw_sha256(ledger_raw) != state.get("closed_ledger_sha256")
        or ledger.get("closed_at_utc") != state.get("closed_at_utc")
    ):
        _fail("closed ledger differs from accepted-close authority")


def _prepared_from_state(
    preclose: JsonObject,
    preclose_raw: bytes,
    state: JsonObject,
) -> PreparedAcceptedClose:
    receipt, receipt_raw = receipt_from_state(state)
    closed = copy.deepcopy(preclose)
    closed["state"] = "closed"
    closed["closed_at_utc"] = state["closed_at_utc"]
    closed_raw = canonical_bytes(closed)
    if (
        raw_sha256(preclose_raw) != state.get("preclose_ledger_sha256")
        or raw_sha256(closed_raw) != state.get("closed_ledger_sha256")
        or raw_sha256(receipt_raw) != state.get("receipt_sha256")
    ):
        _fail("accepted-close state cannot reconstruct its target bytes")
    return PreparedAcceptedClose(state, closed, closed_raw, receipt, receipt_raw)


def _validate_historical_terminal(
    ledger: JsonObject,
    state: JsonObject,
    supplied: Path | None,
) -> None:
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    relative = _text(
        state.get("terminal_revalidation_relative_path"),
        "terminal relative path",
    )
    expected = attempt_root / relative
    if supplied is None:
        return
    if supplied != expected:
        _fail("receipt replay terminal differs from historical binding")
    regular_identity(expected, mode=MODE_IMMUTABLE)
    if raw_sha256(expected.read_bytes()) != state.get("terminal_revalidation_sha256"):
        _fail("historical terminal artifact changed after accepted close")


def _terminal_sha(path: Path) -> str:
    artifact, _raw = load_json(path)
    return _text(artifact.get("sha"), "terminal SHA")


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
