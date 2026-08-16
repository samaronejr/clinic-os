"""Freeze the invoked Phase 1A plan into local and tracked immutable inputs."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.testing.isolation_common import (  # noqa: E402, I001
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    ensure_private_directory,
    regular_identity,
    write_no_replace,
)


ARGUMENT_COUNT: Final = 9


@dataclass(frozen=True, slots=True)
class ApprovedPlan:
    """Paths and digest persisted in the ledger's closed plan object."""

    source_kind: str
    path: Path
    sha256: str
    sidecar_path: Path

    def as_json(self) -> JsonObject:
        """Return the exact closed ledger representation."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "sidecar_path": str(self.sidecar_path),
            "source_kind": self.source_kind,
        }


def freeze_plan(
    source: Path,
    authority_root: Path,
    tracked_copy: Path,
    tracked_sidecar: Path,
    *,
    adopt_existing: bool = False,
) -> ApprovedPlan:
    """Publish no-replace byte-identical local and tracked plan inputs."""
    source_path = _absolute_regular(source, "approved plan source")
    root = _absolute_directory(authority_root, "authority root")
    if root.name != ".omo":
        message = "authority root basename must be .omo"
        raise IsolationError(message)
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    evidence = root / "evidence"
    if not evidence.exists():
        ensure_private_directory(evidence)
    else:
        _absolute_directory(evidence, "evidence root")
    review_inputs = evidence / "review-inputs"
    ensure_private_directory(review_inputs)
    frozen = review_inputs / "approved-plan.md"
    frozen_sidecar = review_inputs / "approved-plan.sha256"
    tracked_copy.parent.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
    if tracked_copy.parent.is_symlink():
        message = "tracked plan parent cannot be a symlink"
        raise IsolationError(message)
    sidecar = f"{digest}\n".encode()
    targets = (
        (frozen, raw, MODE_IMMUTABLE),
        (frozen_sidecar, sidecar, MODE_PRIVATE),
        (tracked_copy, raw, MODE_IMMUTABLE),
        (tracked_sidecar, sidecar, MODE_PRIVATE),
    )
    if adopt_existing:
        for path, expected, mode in targets:
            if path.exists() or path.is_symlink():
                _validate_existing(path, expected, mode)
    for path, expected, mode in targets:
        if not path.exists():
            write_no_replace(path, expected, mode=mode)
    return ApprovedPlan(
        source_kind="invoked",
        path=frozen,
        sha256=digest,
        sidecar_path=frozen_sidecar,
    )


def verify_plan(plan: ApprovedPlan, tracked_copy: Path, tracked_sidecar: Path) -> None:
    """Re-hash local and tracked plan copies plus both sidecars."""
    regular_identity(plan.path, mode=MODE_IMMUTABLE)
    regular_identity(plan.sidecar_path, mode=MODE_PRIVATE)
    regular_identity(tracked_copy, mode=MODE_IMMUTABLE)
    regular_identity(tracked_sidecar, mode=MODE_PRIVATE)
    raw = plan.path.read_bytes()
    sidecar = f"{hashlib.sha256(raw).hexdigest()}\n".encode()
    if (
        hashlib.sha256(raw).hexdigest() != plan.sha256
        or tracked_copy.read_bytes() != raw
        or plan.sidecar_path.read_bytes() != sidecar
        or tracked_sidecar.read_bytes() != sidecar
    ):
        message = "approved plan copy or sidecar drifted"
        raise IsolationError(message)


def _absolute_regular(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        message = f"{label} must be an absolute non-symlink file"
        raise IsolationError(message)
    resolved = path.resolve(strict=True)
    value = resolved.stat(follow_symlinks=False)
    if resolved != path or not stat.S_ISREG(value.st_mode):
        message = f"{label} is noncanonical or nonregular"
        raise IsolationError(message)
    return resolved


def _validate_existing(path: Path, expected: bytes, mode: int) -> None:
    regular_identity(path, mode=mode)
    if path.read_bytes() != expected:
        message = f"existing approved-plan artifact differs: {path.name}"
        raise IsolationError(message)


def _absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        message = f"{label} must be an absolute non-symlink directory"
        raise IsolationError(message)
    resolved = path.resolve(strict=True)
    value = resolved.stat(follow_symlinks=False)
    if (
        resolved != path
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        message = f"{label} is not an executor-owned canonical directory"
        raise IsolationError(message)
    return resolved


def _parse_command() -> tuple[Path, Path, Path, Path]:
    arguments = sys.argv[1:]
    expected = (
        "freeze",
        "--source",
        "--authority-root",
        "--tracked-copy",
        "--tracked-sidecar",
    )
    received = tuple(arguments[index] for index in (0, 1, 3, 5, 7))
    if len(arguments) != ARGUMENT_COUNT or received != expected:
        message = "invalid approved-plan command grammar"
        raise IsolationError(message)
    return (
        Path(arguments[2]),
        Path(arguments[4]),
        Path(arguments[6]),
        Path(arguments[8]),
    )


def _main() -> int:
    try:
        source, authority_root, tracked_copy, tracked_sidecar = _parse_command()
        freeze_plan(source, authority_root, tracked_copy, tracked_sidecar)
    except (
        IsolationError,
        FileExistsError,
        FileNotFoundError,
        PermissionError,
    ) as error:
        print(f"approved-plan: {error}", file=sys.stderr)  # noqa: T201
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
