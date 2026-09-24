"""Bounded object-store boundary for quarantined clinical attachment bytes.

The synthetic backend is a local directory addressed only by opaque keys the
service generates; callers never supply paths and keys are never authority.
``put`` and ``get`` are bounded local operations so no long-running provider
work happens inside the request transaction. A production deployment must
substitute an approved storage and encryption capability.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from config.settings.contracts import LIVE_DATA_MODE
from django.conf import settings
from ops.release.activation import (
    LiveModeHaltedError,
    StorageOwnershipError,
    authorize_live_storage_mutation,
    claim_synthetic_storage,
)

KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
# Reads stay bounded even if a stored object exceeds the upload contract.
MAX_OBJECT_BYTES = 10 * 1024 * 1024
RECEIPT_SUFFIX = ".pending"


class AttachmentStorageError(Exception):
    """Report a storage-boundary failure without exposing object details."""


@dataclass(frozen=True)
class PendingUpload:
    """A durable receipt for an object whose transaction has not committed.

    The receipt survives request-transaction rollback and process crashes, so
    a stored object is never untracked: reconciliation can always account for
    it. ``organization_id`` scopes the metadata existence check.
    """

    key: str
    organization_id: str


class AttachmentStorage(Protocol):
    """Store and materialize immutable byte objects under opaque keys."""

    def put(self, key: str, data: bytes) -> None:
        """Persist bytes under one new key; existing keys are never replaced."""
        ...

    def replace(self, key: str, data: bytes) -> None:
        """Atomically swap the bytes under one existing key.

        Only the owner-side object migration uses this: re-encrypting a
        stored object in place keeps the row's immutable ``storage_key``
        binding intact. A missing key is a storage failure, never a create.
        """
        ...

    def get(self, key: str) -> bytes:
        """Materialize the complete object, bounded by the upload contract."""
        ...

    def delete(self, key: str) -> None:
        """Remove an object during cleanup; a missing object is already gone."""
        ...

    def mark_pending(self, key: str, organization_id: str) -> None:
        """Write a durable receipt before the object so rollback stays tracked."""
        ...

    def clear_pending(self, key: str) -> None:
        """Remove the receipt once the owning transaction has committed."""
        ...

    def pending_uploads(self) -> list[PendingUpload]:
        """List receipts left by rolled-back or interrupted uploads."""
        ...


class FilesystemAttachmentStorage:
    """Synthetic object store: one file per opaque key under a private root."""

    def __init__(self, root: Path) -> None:
        """Bind the adapter to its configured root directory."""
        self._root = root

    def _authorize_mutation(self) -> None:
        """Enforce the storage-ownership and live-rollback boundaries.

        Synthetic mutations claim the root with an ownership marker and
        refuse a root claimed by a live activation; live mutations require
        the activation to still authorize this process and to own this
        root, so ``disable`` stops new writes without a restart.
        """
        try:
            if getattr(settings, "CLINIC_DATA_MODE", None) == LIVE_DATA_MODE:
                authorize_live_storage_mutation(os.environ, self._root)
            else:
                # The ownership marker is a sibling of the root, so the
                # root must exist before a claim write lands.
                self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
                claim_synthetic_storage(self._root)
        except (LiveModeHaltedError, OSError, StorageOwnershipError) as error:
            raise AttachmentStorageError from error

    def _path(self, key: str) -> Path:
        if KEY_PATTERN.fullmatch(key) is None:
            raise AttachmentStorageError
        return self._root / key

    def put(self, key: str, data: bytes) -> None:
        """Write once with exclusive create; a collision is a storage failure."""
        path = self._path(key)
        self._authorize_mutation()
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
            except BaseException:
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
        except OSError as error:
            raise AttachmentStorageError from error

    def get(self, key: str) -> bytes:
        """Read the whole object only after proving it fits the bound."""
        path = self._path(key)
        try:
            if path.stat().st_size > MAX_OBJECT_BYTES:
                raise AttachmentStorageError
            return path.read_bytes()
        except OSError as error:
            raise AttachmentStorageError from error

    def replace(self, key: str, data: bytes) -> None:
        """Atomically rename a staged sibling over the existing object.

        The staged file lives in the same directory so the rename is a
        same-filesystem atomic swap; a missing target or any I/O failure
        removes the stage and reports a storage failure.
        """
        path = self._path(key)
        self._authorize_mutation()
        stage = self._root / f"{key}.replace-stage"
        try:
            if not path.is_file() or path.is_symlink():
                raise AttachmentStorageError
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                with contextlib.suppress(OSError):
                    stage.unlink()
                raise
            stage.replace(path)
        except OSError as error:
            with contextlib.suppress(OSError):
                stage.unlink()
            raise AttachmentStorageError from error

    def delete(self, key: str) -> None:
        """Best-effort removal; absence is success, other errors are reported."""
        self._authorize_mutation()
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            raise AttachmentStorageError from error

    def _receipt_path(self, key: str) -> Path:
        self._path(key)  # Reuse the key validation; receipts never add paths.
        return self._root / f"{key}{RECEIPT_SUFFIX}"

    def mark_pending(self, key: str, organization_id: str) -> None:
        """Record the owning organization durably before the object exists."""
        path = self._receipt_path(key)
        self._authorize_mutation()
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(organization_id, encoding="ascii")
        except OSError as error:
            raise AttachmentStorageError from error

    def clear_pending(self, key: str) -> None:
        """Drop the receipt; a missing receipt is already cleared."""
        self._authorize_mutation()
        try:
            self._receipt_path(key).unlink(missing_ok=True)
        except OSError as error:
            raise AttachmentStorageError from error

    def pending_uploads(self) -> list[PendingUpload]:
        """Enumerate receipts; each names its object key and organization."""
        try:
            entries = sorted(self._root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise AttachmentStorageError from error
        pending: list[PendingUpload] = []
        for entry in entries:
            if not entry.name.endswith(RECEIPT_SUFFIX):
                continue
            key = entry.name[: -len(RECEIPT_SUFFIX)]
            if KEY_PATTERN.fullmatch(key) is None:
                continue
            try:
                organization_id = entry.read_text(encoding="ascii").strip()
            except OSError as error:
                raise AttachmentStorageError from error
            pending.append(PendingUpload(key=key, organization_id=organization_id))
        return pending


def default_storage() -> AttachmentStorage:
    """Resolve the configured synthetic backend for this process."""
    return FilesystemAttachmentStorage(Path(settings.EHR_ATTACHMENT_ROOT))
