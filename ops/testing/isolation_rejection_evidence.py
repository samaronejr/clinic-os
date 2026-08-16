"""Authenticate fixed rejection receipts and phase artifact hashes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts

if TYPE_CHECKING:
    from pathlib import Path

PHASE_PATHS: Final = (
    "terminal/inputs.json",
    "pre-f4.json",
    "final.json",
)
EVIDENCE_ONLY_CLASSES: Final = frozenset(
    {
        "infrastructure",
        "authentication",
        "session",
        "timeout",
        "malformed-output",
    }
)


def receipt_rejections(
    ledger: JsonObject,
    sha: str,
    attempt_root: Path | None = None,
) -> tuple[list[JsonObject], str, tuple[str, ...], str]:
    """Derive the only legal reasons, aggregate, and retry kind from receipts."""
    receipts, aggregate = load_failure_receipts(ledger, attempt_root)
    if aggregate is None or not receipts:
        _fail("non-USER rejection requires authenticated failure receipts")
    rejections: list[JsonObject] = []
    for receipt in receipts:
        if receipt.get("sha") != sha:
            _fail("failure receipt SHA differs from rejected SHA")
        lane = receipt.get("lane")
        failure_class = receipt.get("failure_class")
        if not isinstance(lane, str) or not isinstance(failure_class, str):
            _fail("failure receipt reason fields are invalid")
        rejections.append({"failure_class": failure_class, "lane": lane})
    rejections.sort(key=lambda item: (str(item["lane"]), str(item["failure_class"])))
    if len({(item["lane"], item["failure_class"]) for item in rejections}) != len(
        rejections
    ):
        _fail("failure receipts derive duplicate rejection reasons")
    reasons = tuple(f"{item['lane']}:{item['failure_class']}" for item in rejections)
    classes = {str(item["failure_class"]) for item in rejections}
    retry_kind = "evidence-only" if classes <= EVIDENCE_ONLY_CLASSES else "source-fix"
    return rejections, aggregate, reasons, retry_kind


def phase_hashes(ledger_path: Path, control_root: Path) -> tuple[str | None, ...]:
    """Hash only the three fixed immutable final-wave phase artifacts."""
    expected_root = ledger_path.parent / "clinic-os-phase1a-final"
    if not control_root.is_absolute() or control_root != expected_root:
        _fail("control root is not the canonical final-wave path")
    if not control_root.exists():
        if control_root.is_symlink():
            _fail("absent control root cannot be a symlink")
        return (None, None, None)
    if control_root.is_symlink() or control_root.resolve(strict=True) != control_root:
        _fail("control root is noncanonical")
    return tuple(_optional_hash(control_root / relative) for relative in PHASE_PATHS)


def _optional_hash(path: Path) -> str | None:
    try:
        regular_identity(path, mode=MODE_IMMUTABLE)
    except FileNotFoundError:
        if path.is_symlink():
            _fail("phase artifact path is a dangling symlink")
        return None
    return raw_sha256(path.read_bytes())


def _fail(message: str) -> Never:
    raise IsolationError(message)
