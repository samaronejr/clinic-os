"""Define fixed archive selectors, paths, and state names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_snapshot import LEDGER_NAME, LOCK_NAME

if TYPE_CHECKING:
    from pathlib import Path

ARCHIVE_JOURNAL_NAME: Final = "isolation-archive-rollover-phase1a.json"
ARCHIVE_SENTINEL_NAME: Final = "isolation-archive-rollover-phase1a.sentinel"
UUID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA40_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    """Closed archive selectors that must match the rejected ledger and spec."""

    closed_attempt: str
    rejected_sha: str
    control_root: Path
    rejection_spec_sha256: str


@dataclass(frozen=True, slots=True)
class ArchivePaths:
    """Canonical source, bundle, journal, sentinel, and lock paths."""

    ledger: Path
    lock: Path
    attempt: Path
    control: Path
    journal: Path
    sentinel: Path
    bundle: Path
    bundled_control: Path
    bundled_attempt: Path
    bundled_ledger: Path
    bundled_journal: Path
    tombstone: Path


def archive_paths(ledger_path: Path, request: ArchiveRequest) -> ArchivePaths:
    """Validate caller selectors and derive every archive path internally."""
    if (
        not ledger_path.is_absolute()
        or ledger_path.name != LEDGER_NAME
        or ledger_path.is_symlink()
    ):
        _fail("archive ledger path is not canonical")
    evidence_root = ledger_path.parent
    if (
        evidence_root.is_symlink()
        or evidence_root.resolve(strict=True) != evidence_root
    ):
        _fail("archive evidence root is noncanonical")
    if UUID_PATTERN.fullmatch(request.closed_attempt) is None:
        _fail("closed attempt must be a canonical UUID")
    if SHA40_PATTERN.fullmatch(request.rejected_sha) is None:
        _fail("rejected SHA must be lowercase 40-hex")
    if SHA256_PATTERN.fullmatch(request.rejection_spec_sha256) is None:
        _fail("rejection spec SHA must be lowercase 64-hex")
    expected_control = evidence_root / "clinic-os-phase1a-final"
    if (
        request.control_root != expected_control
        or not request.control_root.is_absolute()
    ):
        _fail("archive control root is not canonical")
    attempt = evidence_root / "clinic-os-phase1a-runtime" / request.closed_attempt
    bundle = (
        evidence_root
        / "clinic-os-phase1a-rejected"
        / request.closed_attempt
        / request.rejected_sha
    )
    return ArchivePaths(
        ledger=ledger_path,
        lock=evidence_root / LOCK_NAME,
        attempt=attempt,
        control=request.control_root,
        journal=evidence_root / ARCHIVE_JOURNAL_NAME,
        sentinel=evidence_root / ARCHIVE_SENTINEL_NAME,
        bundle=bundle,
        bundled_control=bundle / "control",
        bundled_attempt=bundle / "attempt",
        bundled_ledger=bundle / "ledger.closed.json",
        bundled_journal=bundle / "archive-state.json",
        tombstone=bundle / "rejection.json",
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
