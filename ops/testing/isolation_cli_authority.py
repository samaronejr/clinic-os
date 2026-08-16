"""Resolve fixed CLI evidence selectors from the authenticated ledger root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from pathlib import Path

RELATIVE_CONTROL_ROOT: Final = ".omo/evidence/clinic-os-phase1a-final"
RELATIVE_FINAL_RECEIPT: Final = ".omo/evidence/isolation-ledger-final-phase1a.json"


def canonical_control_root(ledger_path: Path, argument: str) -> Path:
    """Accept only the plan's relative spelling or its derived absolute path."""
    expected = ledger_path.parent / "clinic-os-phase1a-final"
    if argument not in (RELATIVE_CONTROL_ROOT, str(expected)):
        _fail("control root is not the canonical final-wave path")
    return expected


def canonical_final_receipt(ledger_path: Path, argument: str) -> Path:
    """Resolve only the retained accepted-success receipt path."""
    expected = ledger_path.parent / "isolation-ledger-final-phase1a.json"
    if argument not in (RELATIVE_FINAL_RECEIPT, str(expected)):
        _fail("final receipt is not the canonical accepted-success path")
    return expected


def canonical_terminal_final(ledger_path: Path, argument: str) -> Path:
    """Resolve only the absolute immutable FINAL selector."""
    expected = ledger_path.parent / "clinic-os-phase1a-final" / "final.json"
    if argument != str(expected):
        _fail("terminal FINAL is not its canonical absolute path")
    return expected


def _fail(message: str) -> Never:
    raise IsolationError(message)
