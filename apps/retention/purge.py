"""Key destruction precedes external deletion; a receipt follows proved absence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from apps.ehr.attachment_storage import AttachmentStorageError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgePlan:
    """Committed tombstone and honest backup horizon; keys never enter logs."""

    purge_id: UUID
    object_keys: tuple[str, ...] = field(repr=False)
    backup_copy_expires_at: datetime


class PurgeStorage(Protocol):
    """Deletion alone is not evidence that the object is absent."""

    def delete(self, key: str) -> None:
        """Delete one object idempotently."""
        ...

    def clear_pending(self, key: str) -> None:
        """Remove its reconciliation marker."""
        ...

    def absent(self, key: str) -> bool:
        """Prove object and pending marker absence, or raise on uncertainty."""
        ...


class PurgeBackend(Protocol):
    """Every method owns and commits its own authenticated transaction."""

    def prepare(self, purge_id: UUID) -> PurgePlan | None:
        """Recheck policy and holds, then commit destruction of decrypting keys."""
        ...

    def confirm(self, purge_id: UUID, key: str) -> None:
        """Record the absence of one exact stored object target."""
        ...

    def complete(self, purge_id: UUID) -> None:
        """Refuse completion until every target has an absence confirmation."""
        ...

    def fail(self, purge_id: UUID, reason: str) -> None:
        """Retain purging state and create one durable operator task."""
        ...


def reapply_object_tombstones(
    *, storage: PurgeStorage, object_keys: Sequence[str]
) -> int:
    """Erase resurrected backup objects; uncertainty prevents restore exposure."""
    for key in object_keys:
        storage.delete(key)
        storage.clear_pending(key)
        if not storage.absent(key):
            raise AttachmentStorageError
    return len(object_keys)


def execute_purge(
    *, purge_id: UUID, backend: PurgeBackend, storage: PurgeStorage | None
) -> PurgePlan | None:
    """Run the external leg only after crypto-shred has durably committed."""
    plan = backend.prepare(purge_id)
    if plan is None:
        return None
    if plan.object_keys and storage is None:
        backend.fail(purge_id, "storage_unavailable")
        return None
    try:
        if storage is not None:
            for key in plan.object_keys:
                reapply_object_tombstones(storage=storage, object_keys=(key,))
                backend.confirm(purge_id, key)
    except AttachmentStorageError:
        backend.fail(purge_id, "storage_unavailable")
        return None
    backend.complete(purge_id)
    return plan
