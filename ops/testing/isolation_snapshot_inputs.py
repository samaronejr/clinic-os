"""Publish or authenticate immutable first-attempt snapshot inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    canonical_bytes,
    directory_identity,
    ensure_private_directory,
    load_json,
    regular_identity,
    write_no_replace,
)
from ops.testing.isolation_lineage import first_lineage_seed_record

if TYPE_CHECKING:
    from pathlib import Path


def publish_attempt_inputs(
    attempt_root: Path,
    shared_path: Path,
    shared_raw: bytes,
    attempt_id: str,
    *,
    recovering: bool,
) -> None:
    """Create fresh inputs or validate an exact interrupted prefix."""
    _ensure_directory(attempt_root.parent, allow_existing=True)
    _ensure_directory(attempt_root, allow_existing=recovering)
    _ensure_directory(
        attempt_root / "final-failure-receipts",
        allow_existing=recovering,
    )
    _ensure_directory(attempt_root / "todo-evidence", allow_existing=recovering)
    _publish_or_validate(shared_path, shared_raw, recovering=recovering)
    seed_raw = canonical_bytes(first_lineage_seed_record(attempt_id))
    _publish_or_validate(
        attempt_root / "receipt-lineage-seed.json",
        seed_raw,
        recovering=recovering,
    )


def _ensure_directory(path: Path, *, allow_existing: bool) -> None:
    if path.exists() or path.is_symlink():
        if not allow_existing:
            _fail(f"snapshot input directory already exists: {path.name}")
        identity = directory_identity(path)
        if identity["mode"] != MODE_DIRECTORY:
            _fail(f"snapshot input directory mode drifted: {path.name}")
        return
    ensure_private_directory(path)
    if directory_identity(path)["mode"] != MODE_DIRECTORY:
        _fail(f"snapshot input directory mode drifted: {path.name}")


def _publish_or_validate(path: Path, raw: bytes, *, recovering: bool) -> None:
    if path.exists() or path.is_symlink():
        if not recovering:
            _fail(f"snapshot input already exists: {path.name}")
        regular_identity(path, mode=MODE_IMMUTABLE)
        _value, existing = load_json(path)
        if existing != raw:
            _fail(f"snapshot input bytes drifted: {path.name}")
        return
    write_no_replace(path, raw, mode=MODE_IMMUTABLE)


def _fail(message: str) -> Never:
    raise IsolationError(message)
