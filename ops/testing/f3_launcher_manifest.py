"""Freeze the absolute F3 virtualenv launcher and four checksum records."""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from pathlib import Path

FROZEN_MODE: Final = 0o400


class F3LauncherManifestError(RuntimeError):
    """Reject launcher path, lstat, target, script, or publication drift."""


def _fail(reason: str) -> Never:
    raise F3LauncherManifestError(reason)


def freeze_launcher_sidecars(
    *,
    inputs: Path,
    launcher: Path,
    shell: Path,
    supervisor: Path,
    controller: Path,
) -> tuple[Path, Path, Path, Path]:
    """Publish the exact mode-0400 launcher chain beside frozen inputs."""
    _immutable(inputs)
    path_raw, lstat_raw, launcher_hash_raw = launcher_prefix_bytes(
        launcher, shell, supervisor, controller
    )
    terminal = inputs.parent
    path_file = terminal / "F3-launcher.path"
    lstat_file = terminal / "F3-launcher.lstat"
    launcher_hash_file = terminal / "F3-launcher.sha256"
    inputs_hash_file = terminal / "inputs.sha256"
    _publish(path_file, path_raw)
    _publish(lstat_file, lstat_raw)
    _publish(launcher_hash_file, launcher_hash_raw)
    _publish(
        inputs_hash_file,
        input_sidecar_bytes(inputs, path_file, lstat_file, launcher_hash_file),
    )
    return path_file, lstat_file, launcher_hash_file, inputs_hash_file


def launcher_prefix_bytes(
    launcher: Path,
    shell: Path,
    supervisor: Path,
    controller: Path,
) -> tuple[bytes, bytes, bytes]:
    """Build the path, lstat, and four-record launcher bytes without writing."""
    if (
        not launcher.is_absolute()
        or not launcher.is_symlink()
        or not os.access(launcher, os.X_OK)
        or not launcher.as_posix().endswith("/.venv/bin/python")
    ):
        _fail("F3 launcher identity rejected")
    scripts = (shell, supervisor, controller)
    if any(
        not item.is_absolute() or item.is_symlink() or not item.is_file()
        for item in scripts
    ):
        _fail("F3 launcher script identity rejected")
    status = os.lstat(launcher)
    target = os.fspath(launcher.readlink())
    if not target or "\n" in target or "\r" in target or "\x00" in target:
        _fail("F3 launcher link target rejected")
    target_sha256 = hashlib.sha256(os.fsencode(target)).hexdigest()
    lstat = (
        f"{status.st_dev} {status.st_ino} {status.st_mode:x} {status.st_uid} "
        f"{status.st_gid} {status.st_nlink} {target_sha256}\n"
    ).encode("ascii")
    resolved = launcher.resolve(strict=True)
    if (
        resolved.is_symlink()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        _fail("F3 resolved launcher target rejected")
    return (
        f"{launcher}\n".encode(),
        lstat,
        _checksum_records((resolved, *scripts)),
    )


def input_sidecar_bytes(
    inputs: Path,
    path_file: Path,
    lstat_file: Path,
    launcher_hash_file: Path,
) -> bytes:
    """Build the sorted four-record checksum over all frozen launcher inputs."""
    return _checksum_records((lstat_file, path_file, launcher_hash_file, inputs))


def _checksum_records(paths: tuple[Path, ...]) -> bytes:
    sorted_paths = sorted(paths, key=lambda item: item.as_posix())
    if len(sorted_paths) != len(set(sorted_paths)):
        _fail("F3 launcher checksum paths are duplicated")
    records = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n"
        for path in sorted_paths
    ]
    return "".join(records).encode("utf-8")


def _publish(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        _immutable(path)
        if path.read_bytes() != raw:
            _fail("F3 launcher publication drifted")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.fchmod(descriptor, FROZEN_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync(path.parent)


def _immutable(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o777 != FROZEN_MODE
        or path.stat().st_nlink != 1
    ):
        _fail("F3 launcher frozen file identity rejected")


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
