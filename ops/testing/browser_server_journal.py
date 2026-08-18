"""Durable hash-chained barrier journal for the browser-server session."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Never

import rfc8785

if TYPE_CHECKING:
    from pathlib import Path

from ops.testing.browser_server_stages import (
    SEALED,
    BrowserStageError,
    is_complete,
    require_known_stage,
    require_recordable,
)

RECORD_KEYS: Final = frozenset(
    {"chain_sha256", "previous_sha256", "recorded_at_utc", "sequence", "stage"}
)
GENESIS: Final = "0" * 64
MODE_PRIVATE: Final = 0o600


class BrowserJournalError(RuntimeError):
    """Reject a corrupt, replayed, or unsealed browser-server journal."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying journal failure."""
        super().__init__(f"browser journal rejected: {reason}")


def _fail(reason: str) -> Never:
    raise BrowserJournalError(reason)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _chain(previous: str, sequence: int, stage: str, moment: str) -> str:
    payload = rfc8785.dumps(
        {
            "previous_sha256": previous,
            "recorded_at_utc": moment,
            "sequence": sequence,
            "stage": stage,
        }
    )
    return hashlib.sha256(payload).hexdigest()


class BarrierJournal:
    """Append one fsynced hash-chained record per causally ordered stage."""

    def __init__(self, path: Path) -> None:
        """Bind the journal to one absolute executor-owned path."""
        if not path.is_absolute():
            _fail("journal path must be absolute")
        self.path = path
        self._records: list[dict[str, object]] = []
        if path.exists():
            self._records = list(read_records(path))

    @property
    def recorded(self) -> tuple[str, ...]:
        """Return every stage already recorded, in journal order."""
        return tuple(str(record["stage"]) for record in self._records)

    @property
    def sealed(self) -> bool:
        """Report whether the terminal stage has been recorded."""
        return SEALED in self.recorded

    def record(self, stage: str) -> str:
        """Append one durable record after its predecessors are proven."""
        require_recordable(require_known_stage(stage), self.recorded)
        if stage == SEALED and not is_complete((*self.recorded, SEALED)):
            _fail("cannot seal an incomplete session")
        sequence = len(self._records)
        previous = (
            GENESIS if not self._records else str(self._records[-1]["chain_sha256"])
        )
        moment = _timestamp()
        record: dict[str, object] = {
            "chain_sha256": _chain(previous, sequence, stage, moment),
            "previous_sha256": previous,
            "recorded_at_utc": moment,
            "sequence": sequence,
            "stage": stage,
        }
        self._append(record)
        self._records.append(record)
        return str(record["chain_sha256"])

    def _append(self, record: dict[str, object]) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            MODE_PRIVATE,
        )
        try:
            os.write(descriptor, line.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def require_stage(self, stage: str) -> None:
        """Require one already-recorded predecessor before an external effect."""
        if require_known_stage(stage) not in self.recorded:
            _fail(f"{stage} has not been recorded")


def read_records(path: Path) -> tuple[dict[str, object], ...]:
    """Replay and validate one journal, proving its hash chain and order."""
    records: list[dict[str, object]] = []
    previous = GENESIS
    seen: list[str] = []
    for index, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
    ):
        document: object = json.loads(raw)
        if not isinstance(document, dict) or set(document) != set(RECORD_KEYS):
            _fail("journal record has the wrong closed key set")
        record = {str(key): value for key, value in document.items()}
        stage = str(record["stage"])
        moment = str(record["recorded_at_utc"])
        if record["sequence"] != index or record["previous_sha256"] != previous:
            _fail("journal chain is discontinuous")
        if record["chain_sha256"] != _chain(previous, index, stage, moment):
            _fail("journal record digest does not match its content")
        try:
            require_recordable(stage, tuple(seen))
        except BrowserStageError as error:
            raise BrowserJournalError(str(error)) from error
        seen.append(stage)
        previous = str(record["chain_sha256"])
        records.append(record)
    return tuple(records)


def resume(path: Path) -> BarrierJournal:
    """Return the journal a successor may continue after an abrupt exit."""
    journal = BarrierJournal(path)
    if journal.sealed:
        _fail("sealed journal cannot be resumed")
    return journal
