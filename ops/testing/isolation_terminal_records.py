"""Define the closed terminal-revalidation interface records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject


@dataclass(frozen=True, slots=True)
class ValidatedTerminalRevalidation:
    """Authenticated terminal, FINAL, and released-publisher evidence."""

    artifact: JsonObject
    artifact_path: Path
    artifact_raw: bytes
    final_raw: bytes
    publisher_journal_raw: bytes


@dataclass(frozen=True, slots=True)
class TerminalValidationInputs:
    """Closed ledger and selector inputs for terminal-chain validation."""

    ledger_path: Path
    ledger: JsonObject
    ledger_raw: bytes
    sha: str
    path: Path
    control_root: Path
