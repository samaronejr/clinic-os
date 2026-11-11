"""Recover tenant keys from escrow and objects from backup with the rows (ADR-016).

A restore that brings back rows without the KEK that unwraps their DEKs, or
without the attachment objects their rows reference, looks green while being
unusable. Every check here therefore runs before the target is mutated where
it can, and each gap fails the drill with one explicit message.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import TYPE_CHECKING, Final, Never, Protocol

from ops.testing.restore_contract import RestoreContractError
from ops.testing.restore_queries import ATTACHMENT_MANIFEST_SQL
from ops.testing.restore_verification import require_key_probe

if TYPE_CHECKING:
    from pathlib import Path

KEK_SECRET_FILE: Final = "tenant-kek.secret"  # noqa: S105 - a file name
RESTORED_SECRETS_DIRECTORY: Final = "secrets-restored"
PRIVATE_DIRECTORY_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600
_KEK_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
_OBJECT_KEY_PATTERN: Final = re.compile(r"(?:orders/)?[0-9a-f]{64}")

MISSING_ESCROW: Final = "restore drill failed: the tenant KEK escrow is missing"
UNREADABLE_ESCROW: Final = "restore drill failed: the tenant KEK escrow is unreadable"
STALE_ESCROW: Final = (
    "restore drill failed: the escrowed tenant KEK cannot open the source keys"
)
MISSING_BACKUP: Final = "restore drill failed: the object backup is missing"


class _SqlClient(Protocol):
    def sql(self, statement: str) -> str: ...


class _KeyBinding(Protocol):
    @property
    def organization_id(self) -> str: ...
    @property
    def envelope_hex(self) -> str: ...
    @property
    def expected_sha256(self) -> str: ...


def read_key_escrow(escrow: Path) -> str:
    """Return the escrowed tenant KEK; absence or a damaged copy fails the drill."""
    path = escrow / KEK_SECRET_FILE
    try:
        if (
            not escrow.is_absolute()
            or escrow.is_symlink()
            or not escrow.is_dir()
            or path.is_symlink()
            or not path.is_file()
        ):
            _fail(MISSING_ESCROW)
        if (
            escrow.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
            or path.stat().st_mode & 0o077
        ):
            _fail("restore drill failed: the tenant KEK escrow is not private")
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        _fail(UNREADABLE_ESCROW)
    if _KEK_PATTERN.fullmatch(value) is None:
        _fail(UNREADABLE_ESCROW)
    return value


def require_escrow_opens_source(
    source: _SqlClient, binding: _KeyBinding, kek: str
) -> None:
    """Prove the escrowed KEK still unwraps the live source DEKs before restoring.

    A KEK rotation that never reached the escrow leaves a backup nobody can
    read; this refuses it while the target is still empty.
    """
    try:
        require_key_probe(
            source,
            organization_id=binding.organization_id,
            kek=kek,
            envelope_hex=binding.envelope_hex,
            expected_sha256=binding.expected_sha256,
        )
    except RestoreContractError:
        _fail(STALE_ESCROW)


def require_object_backup(source: _SqlClient, backup: Path) -> int:
    """Require one backup object per attachment row the source references."""
    if not backup.is_absolute() or backup.is_symlink() or not backup.is_dir():
        _fail(MISSING_BACKUP)
    keys: list[str] = []
    for line in source.sql(ATTACHMENT_MANIFEST_SQL).splitlines():
        if not line.strip():
            continue
        key = line.strip().split("|", 1)[0]
        if _OBJECT_KEY_PATTERN.fullmatch(key) is None:
            _fail("attachment manifest row is invalid")
        keys.append(key)
    missing = [
        key
        for key in keys
        if (backup / key).is_symlink() or not (backup / key).is_file()
    ]
    if missing:
        _fail(
            f"restore drill failed: {len(missing)} attachment object(s) are "
            "missing from the object backup"
        )
    return len(keys)


def restore_object_backup(backup: Path, destination: Path) -> None:
    """Copy the object backup into the fresh, empty restored object store."""
    try:
        if (
            not destination.is_absolute()
            or destination.is_symlink()
            or not destination.is_dir()
            or destination.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
            or any(destination.iterdir())
            or destination.resolve().is_relative_to(backup.resolve())
        ):
            _fail("restored object store root is invalid")
        shutil.copytree(backup, destination, symlinks=True, dirs_exist_ok=True)
    except OSError:
        _fail("restore drill failed: the object backup could not be restored")


def restore_key_escrow(kek: str, work_dir: Path) -> Path:
    """Install the escrowed KEK as the restored secret store; return its root."""
    if _KEK_PATTERN.fullmatch(kek) is None:
        _fail(UNREADABLE_ESCROW)
    root = work_dir / RESTORED_SECRETS_DIRECTORY
    try:
        root.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        root.chmod(PRIVATE_DIRECTORY_MODE)
        descriptor = os.open(
            root / KEK_SECRET_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
        )
        try:
            os.write(descriptor, kek.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("restore drill failed: the escrowed tenant KEK could not be restored")
    return root


def delete_restored_secrets(root: Path) -> bool:
    """Remove the restored secret store after verification; True when absent."""
    path = root / KEK_SECRET_FILE
    try:
        if path.exists() and not path.is_symlink():
            path.unlink()
        if root.exists() and not root.is_symlink():
            root.rmdir()
    except OSError:
        return False
    return not root.exists()


def _fail(message: str) -> Never:
    raise RestoreContractError(message)
