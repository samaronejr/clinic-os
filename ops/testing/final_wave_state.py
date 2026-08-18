"""Persist and validate final-wave publisher phase checkpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Never

from ops.testing.isolation_common import (
    IsolationError,
    canonical_bytes,
    utc_now,
    write_atomic_replace,
)
from ops.testing.isolation_final_wave_controller import (
    FinalWaveSession,
    acquire_final_wave,
    require_failure_receipt,
)
from ops.testing.isolation_final_wave_record import (
    FinalWaveStart,
    PreF4Evidence,
    validate_final_wave_record,
)
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_terminal_publisher_contract import (
    validate_final_wave_journal,
)
from ops.testing.isolation_terminal_publisher_journal import (
    load_terminal_publisher_journal,
    validate_initial_publisher_journal,
)


def record_inputs_frozen(ledger_path: Path, digest: str) -> None:
    """Advance the active publisher journal through immutable input freeze."""
    _transition(ledger_path, "inputs-frozen", {"inputs_sha256": digest})


def record_pre_f4_frozen(ledger_path: Path, digest: str) -> None:
    """Advance through lanes-complete and immutable pre-F4 freeze."""
    _transition(ledger_path, "lanes-complete", {})
    _transition(ledger_path, "pre-f4-frozen", {"pre_f4_sha256": digest})


def record_f4_created(ledger_path: Path, digest: str, *, approved: bool) -> None:
    """Bind the publisher-created F4 decision to its semantic phase."""
    phase = "f4-created" if approved else "f4-rejected"
    _transition(ledger_path, phase, {"f4_sha256": digest})


def record_final_frozen(ledger_path: Path, digest: str) -> None:
    """Bind final.json and advance the publisher journal to final-frozen."""
    _transition(ledger_path, "final-frozen", {"final_sha256": digest})


def _transition(ledger_path: Path, phase: str, changes: dict[str, str]) -> None:
    with locked_open_ledger(ledger_path) as session:
        claims = session.ledger.get("claims")
        if not isinstance(claims, list):
            _fail("final-wave ledger claims are invalid")
        publishers = [
            item
            for item in claims
            if isinstance(item, dict)
            and item.get("purpose") == "final-terminal-publisher"
        ]
        if len(publishers) != 1:
            _fail("final-wave publisher is missing or duplicated")
        attempt_root = Path(str(session.ledger["attempt_root"]))
        path = attempt_root / "final-wave-state.json"
        journal, _ = load_terminal_publisher_journal(path)
        _ = validate_initial_publisher_journal(session.ledger, publishers[0], journal)
        current = str(journal["phase"])
        allowed = {
            "inputs-frozen": {"initializing", "inputs-frozen"},
            "lanes-complete": {"inputs-frozen", "lanes-complete"},
            "pre-f4-frozen": {"lanes-complete", "pre-f4-frozen"},
            "f4-created": {"pre-f4-frozen", "f4-created"},
            "f4-rejected": {"pre-f4-frozen", "f4-rejected"},
            "final-frozen": {"f4-created", "final-frozen"},
        }
        if current not in allowed[phase]:
            _fail("final-wave publisher phase transition is invalid")
        if current == phase:
            if any(journal.get(key) != value for key, value in changes.items()):
                _fail("final-wave publisher phase replay drifted")
            return
        journal.update(changes)
        journal["phase"] = phase
        journal["updated_at_utc"] = utc_now()
        validate_final_wave_journal(journal)
        write_atomic_replace(path, canonical_bytes(journal))


def _fail(message: str) -> Never:
    raise IsolationError(message)


__all__ = [
    "FinalWaveSession",
    "FinalWaveStart",
    "PreF4Evidence",
    "acquire_final_wave",
    "record_f4_created",
    "record_final_frozen",
    "record_inputs_frozen",
    "record_pre_f4_frozen",
    "require_failure_receipt",
    "validate_final_wave_record",
]
