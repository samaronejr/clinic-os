"""Serialize authenticated isolation-ledger transitions under the stable lock."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Never

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    regular_identity,
    stable_lock,
    write_atomic_replace,
)
from ops.testing.isolation_snapshot import LEDGER_NAME, LOCK_NAME
from ops.testing.isolation_snapshot_records import ROOT_KEYS

if TYPE_CHECKING:
    from collections.abc import Iterator

BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
SCHEMA_VERSION: Final = 2


def _fail(message: str) -> Never:
    raise IsolationError(message)


@dataclass(slots=True)
class LedgerSession:
    """Expose one mutable ledger only while its authenticated lock is held."""

    path: Path
    ledger: JsonObject
    original_raw: bytes
    lock_descriptor: int
    commit_count: int = 0

    def commit(self) -> None:
        """Durably replace the ledger after a validated transition."""
        raw = canonical_bytes(self.ledger)
        if raw == self.original_raw:
            _fail("ledger transition made no state change")
        write_atomic_replace(self.path, raw)
        regular_identity(self.path, mode=MODE_PRIVATE)
        self.original_raw = raw
        self.commit_count += 1


@dataclass(frozen=True, slots=True)
class _LedgerRequirement:
    boot_relation: Literal["same", "stale", "recovery", "any"]
    required_state: Literal["open", "either"]


@contextmanager
def locked_open_ledger(path: Path) -> Iterator[LedgerSession]:
    """Authenticate and hold one open same-boot canonical ledger."""
    with _locked_ledger(path, boot_relation="same", required_state="open") as session:
        yield session


@contextmanager
def locked_same_boot_ledger(path: Path) -> Iterator[LedgerSession]:
    """Authenticate and hold one open or closed same-boot canonical ledger."""
    with _locked_ledger(path, boot_relation="same", required_state="either") as session:
        yield session


@contextmanager
def locked_stale_ledger(path: Path) -> Iterator[LedgerSession]:
    """Authenticate and hold one open canonical ledger from a prior boot."""
    with _locked_ledger(path, boot_relation="stale", required_state="open") as session:
        yield session


@contextmanager
def locked_recovery_ledger(path: Path) -> Iterator[LedgerSession]:
    """Hold a recovery ledger across its prior-to-current boot transition."""
    with _locked_ledger(
        path, boot_relation="recovery", required_state="open"
    ) as session:
        yield session


@contextmanager
def locked_any_boot_ledger(path: Path) -> Iterator[LedgerSession]:
    """Hold an open or closed ledger for journal-authenticated replay."""
    with _locked_ledger(path, boot_relation="any", required_state="either") as session:
        yield session


@contextmanager
def _locked_ledger(
    path: Path,
    *,
    boot_relation: Literal["same", "stale", "recovery", "any"],
    required_state: Literal["open", "either"],
) -> Iterator[LedgerSession]:
    requirement = _LedgerRequirement(boot_relation, required_state)
    ledger_path = _canonical_ledger_path(path)
    lock_path = ledger_path.with_name(LOCK_NAME)
    with stable_lock(lock_path, create=False) as (descriptor, held_identity):
        ledger, raw = load_json(ledger_path)
        _validate_open_root(
            ledger,
            ledger_path,
            lock_path,
            held_identity,
            requirement,
        )
        session = LedgerSession(ledger_path, ledger, raw, descriptor)
        yield session


def _canonical_ledger_path(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.name != LEDGER_NAME:
        _fail("ledger path is not the canonical absolute regular file")
    resolved = path.resolve(strict=True)
    if resolved != path:
        _fail("ledger path is noncanonical")
    regular_identity(resolved, mode=MODE_PRIVATE)
    return resolved


def _validate_open_root(
    ledger: JsonObject,
    ledger_path: Path,
    lock_path: Path,
    held_identity: JsonObject,
    requirement: _LedgerRequirement,
) -> None:
    if set(ledger) != ROOT_KEYS or ledger.get("schema_version") != SCHEMA_VERSION:
        _fail("ledger root schema is not the closed version 2 contract")
    state = _validated_state(ledger, requirement.required_state)
    if ledger.get("lock_path") != str(lock_path):
        _fail("ledger stable-lock path changed")
    if ledger.get("lock_identity") != held_identity:
        _fail("ledger stable-lock identity changed")
    _validate_boot_relation(ledger, requirement.boot_relation)
    claims = ledger.get("claims")
    if not isinstance(claims, list) or not all(
        isinstance(item, dict) for item in claims
    ):
        _fail("ledger claims are not a closed object array")
    if state == "closed" and claims != []:
        _fail("closed ledger retained claims")
    attempt_root = ledger.get("attempt_root")
    if not isinstance(attempt_root, str) or not attempt_root.startswith(os.sep):
        _fail("ledger attempt root is not absolute")
    root = Path(attempt_root)
    if root.is_symlink() or root.resolve(strict=True) != root:
        _fail("ledger attempt root is noncanonical")
    if ledger_path.parent not in root.parents:
        _fail("ledger attempt root escaped the authority evidence root")


def _validated_state(
    ledger: JsonObject,
    required_state: Literal["open", "either"],
) -> str:
    state = ledger.get("state")
    closed_at = ledger.get("closed_at_utc")
    if state not in {"open", "closed"}:
        _fail("ledger state is invalid")
    if required_state == "open" and state != "open":
        _fail("ledger is not open")
    if (state == "open" and closed_at is not None) or (
        state == "closed" and not isinstance(closed_at, str)
    ):
        _fail("ledger state and close timestamp disagree")
    return state


def _validate_boot_relation(
    ledger: JsonObject,
    boot_relation: Literal["same", "stale", "recovery", "any"],
) -> None:
    if boot_relation == "any":
        return
    current_boot = BOOT_ID_PATH.read_text().strip()
    same_boot = ledger.get("boot_id") == current_boot
    if (boot_relation == "same" and not same_boot) or (
        boot_relation == "stale" and same_boot
    ):
        _fail(f"ledger does not satisfy the required {boot_relation}-boot relation")
