"""Close a terminally accepted isolation attempt through durable phases."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_accepted_close_records import (
    PHASES,
    prepare_accepted_close,
    receipt_from_state,
    validate_accepted_close_state,
)
from ops.testing.isolation_accepted_close_recovery import (
    AcceptedCloseRecoveryInputs,
    recover_prepared_close,
    validate_closed_close_prefix,
    validate_open_close_prefix,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    utc_now,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_ledger_store import locked_any_boot_ledger

if TYPE_CHECKING:
    from collections.abc import Callable
type Checkpoint = Callable[[str, JsonObject], None]


def close_accepted_attempt(
    ledger_path: Path,
    *,
    final_receipt: Path,
    inventory_reader: Callable[[], JsonObject],
    terminal_revalidation: Path | None = None,
    checkpoint: Checkpoint | None = None,
) -> Path:
    """Close only through terminal-bound ledger-first receipt publication."""
    _validate_receipt_path(ledger_path, final_receipt)
    emit = checkpoint if checkpoint is not None else _no_checkpoint
    with locked_any_boot_ledger(ledger_path) as session:
        attempt_root = Path(_text(session.ledger.get("attempt_root"), "attempt root"))
        state_path = attempt_root / "accepted-close-state.json"
        state, state_raw = _load_optional_state(state_path)
        if state is None:
            if session.ledger.get("state") != "open" or _present(final_receipt):
                _fail("accepted close lacks its required open-ledger prefix")
            terminal = validate_open_close_prefix(
                session.path,
                session.ledger,
                session.original_raw,
                terminal_revalidation,
                inventory_reader,
            )
            timestamp = utc_now()
            prepared = prepare_accepted_close(
                session.ledger,
                session.original_raw,
                terminal,
                closed_at_utc=timestamp,
                updated_at_utc=timestamp,
            )
            write_no_replace(
                state_path,
                canonical_bytes(prepared.state),
                mode=MODE_PRIVATE,
            )
            state, state_raw = load_json(state_path)
            emit("prepared", state)
        else:
            prepared = recover_prepared_close(
                AcceptedCloseRecoveryInputs(
                    session.path,
                    session.ledger,
                    session.original_raw,
                    state_path,
                    state,
                    state_raw,
                    final_receipt,
                    terminal_revalidation,
                    inventory_reader,
                    emit,
                )
            )
            state, state_raw = load_json(state_path)
        if session.ledger.get("state") == "open":
            session.ledger.clear()
            session.ledger.update(prepared.closed_ledger)
            session.commit()
        validate_closed_close_prefix(session.ledger, session.original_raw, state)
        if state.get("phase") == "prepared":
            state = _advance(state_path, state, "ledger-closed", emit)
        if state.get("phase") == "ledger-closed":
            _publish_receipt(final_receipt, state)
            state = _advance(state_path, state, "receipt-published", emit)
        if state.get("phase") == "receipt-published":
            _validate_receipt(final_receipt, state)
            state = _advance(state_path, state, "complete", emit)
        if state.get("phase") != "complete":
            _fail("accepted close did not reach its complete phase")
        validate_closed_close_prefix(session.ledger, session.original_raw, state)
        _validate_receipt(final_receipt, state)
        return final_receipt


def _load_optional_state(path: Path) -> tuple[JsonObject | None, bytes]:
    if not _present(path):
        return None, b""
    regular_identity(path, mode=MODE_PRIVATE)
    state, raw = load_json(path)
    validate_accepted_close_state(state)
    return state, raw


def _advance(
    path: Path,
    state: JsonObject,
    phase: str,
    emit: Checkpoint,
) -> JsonObject:
    current = state.get("phase")
    if current not in PHASES or PHASES.index(phase) != PHASES.index(str(current)) + 1:
        _fail("accepted-close phase transition is nonadjacent")
    updated = copy.deepcopy(state)
    updated["phase"] = phase
    updated["updated_at_utc"] = utc_now()
    write_atomic_replace(path, canonical_bytes(updated))
    observed, _raw = load_json(path)
    emit(phase, observed)
    return observed


def _publish_receipt(path: Path, state: JsonObject) -> None:
    _receipt, expected = receipt_from_state(state)
    try:
        write_no_replace(path, expected, mode=MODE_IMMUTABLE)
    except FileExistsError:
        _validate_receipt(path, state)


def _validate_receipt(path: Path, state: JsonObject) -> None:
    regular_identity(path, mode=MODE_IMMUTABLE)
    _receipt, expected = receipt_from_state(state)
    if path.read_bytes() != expected or raw_sha256(expected) != state.get(
        "receipt_sha256"
    ):
        _fail("final receipt differs from accepted-close authority")


def _validate_receipt_path(ledger_path: Path, path: Path) -> None:
    expected = ledger_path.parent / "isolation-ledger-final-phase1a.json"
    if path != expected or not path.is_absolute():
        _fail("final receipt path is not canonical")


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _no_checkpoint(_stage: str, _state: JsonObject) -> None:
    return


def _fail(message: str) -> Never:
    raise IsolationError(message)
