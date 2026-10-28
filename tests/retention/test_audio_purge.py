"""Exercise irreversible ordering without opening a database connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.retention.purge import PurgePlan, execute_purge, reapply_object_tombstones
from apps.retention.services import apply_retention_policy

if TYPE_CHECKING:
    from uuid import UUID

NOW = datetime(2026, 10, 26, tzinfo=UTC)
KEYS = ("a" * 64, "b" * 64)


@dataclass
class MemoryStore:
    events: list[str] = field(default_factory=list)
    state: str = "purge_due"
    held: bool = False
    dry_run: bool = False
    keys_destroyed: bool = False
    confirmations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def prepare(self, purge_id: UUID) -> PurgePlan | None:
        self.events.append("prepare")
        if self.held or self.dry_run:
            self.state = "blocked_hold" if self.held else self.state
            return None
        self.keys_destroyed = True
        self.state = "purging"
        self.events.append("shred_committed")
        return PurgePlan(
            purge_id=purge_id,
            object_keys=KEYS,
            backup_copy_expires_at=NOW + timedelta(days=35),
        )

    def confirm(self, purge_id: UUID, key: str) -> None:
        assert self.keys_destroyed
        self.events.append("confirm")
        self.confirmations.append(key)

    def complete(self, purge_id: UUID) -> None:
        assert set(self.confirmations) == set(KEYS)
        self.events.append("complete")
        self.state = "purged"

    def fail(self, purge_id: UUID, reason: str) -> None:
        self.failures.append(reason)
        self.events.append("failure_task")


@dataclass
class MemoryStorage:
    store: MemoryStore
    remaining: set[str] = field(default_factory=lambda: set(KEYS))
    fail_on: str = ""
    lie_about_delete: bool = False

    def delete(self, key: str) -> None:
        assert self.store.keys_destroyed
        assert "shred_committed" in self.store.events
        self.store.events.append("delete")
        if key == self.fail_on:
            raise AttachmentStorageError
        if not self.lie_about_delete:
            self.remaining.discard(key)

    def clear_pending(self, key: str) -> None:
        self.store.events.append("clear_pending")

    def absent(self, key: str) -> bool:
        self.store.events.append("verify_absence")
        return key not in self.remaining


def test_shred_commits_before_any_object_delete_and_receipt_follows_all_absences() -> (
    None
):
    store = MemoryStore()
    storage = MemoryStorage(store)
    plan = execute_purge(purge_id=uuid4(), backend=store, storage=storage)
    assert plan is not None
    assert store.state == "purged"
    assert storage.remaining == set()
    assert store.events.index("shred_committed") < store.events.index("delete")
    assert store.events[-1] == "complete"
    assert plan.backup_copy_expires_at == NOW + timedelta(days=35)
    assert store.failures == []


@pytest.mark.parametrize("held", [True, False])
def test_hold_and_default_dry_run_never_destroy_keys_or_objects(held: bool) -> None:
    store = MemoryStore(held=held, dry_run=not held)
    storage = MemoryStorage(store)
    assert execute_purge(purge_id=uuid4(), backend=store, storage=storage) is None
    assert store.keys_destroyed is False
    assert storage.remaining == set(KEYS)
    assert store.confirmations == []
    assert "delete" not in store.events


@pytest.mark.parametrize("lie", [False, True])
def test_failure_stays_purging_and_creates_attention_task(lie: bool) -> None:
    store = MemoryStore()
    storage = MemoryStorage(store, fail_on="" if lie else KEYS[1], lie_about_delete=lie)
    assert execute_purge(purge_id=uuid4(), backend=store, storage=storage) is None
    assert store.state == "purging"
    assert storage.remaining
    assert store.failures == ["storage_unavailable"]
    assert "complete" not in store.events


def test_retry_converges_after_partial_deletion_without_resurrecting_a_key() -> None:
    store = MemoryStore()
    storage = MemoryStorage(store, fail_on=KEYS[1])
    purge_id = uuid4()
    execute_purge(purge_id=purge_id, backend=store, storage=storage)
    assert storage.remaining == {KEYS[1]}
    storage.fail_on = ""
    assert execute_purge(purge_id=purge_id, backend=store, storage=storage)
    assert store.state == "purged"
    assert storage.remaining == set()
    assert store.keys_destroyed


def test_restore_reapplies_object_tombstones_idempotently() -> None:
    store = MemoryStore(keys_destroyed=True, events=["shred_committed"])
    storage = MemoryStorage(store)
    assert reapply_object_tombstones(storage=storage, object_keys=KEYS) == 2
    assert reapply_object_tombstones(storage=storage, object_keys=KEYS) == 2
    assert not storage.remaining


def test_restore_fails_closed_if_any_resurrected_object_remains() -> None:
    store = MemoryStore(keys_destroyed=True, events=["shred_committed"])
    storage = MemoryStorage(store, lie_about_delete=True)
    with pytest.raises(AttachmentStorageError):
        reapply_object_tombstones(storage=storage, object_keys=KEYS)


def test_all_transient_classes_have_exact_public_keyword_only_scope() -> None:
    parameters = signature(apply_retention_policy).parameters
    assert list(parameters) == ["clinic_id", "record_class"]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())
