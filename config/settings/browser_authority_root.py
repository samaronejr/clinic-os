from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_snapshot import LOCK_NAME
from ops.testing.isolation_snapshot_records import ROOT_KEYS

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

EXPECTED_KEYS: Final = frozenset(
    {
        "attempt_id",
        "ca_export_claim_id",
        "database_claim_id",
        "database_name",
        "database_port",
        "environment_contract",
        "foundation_sha",
        "materializer_claim_id",
        "process_claim_id",
        "process_port",
        "project",
        "worktree_realpath",
    }
)
SHA40: Final = re.compile(r"[0-9a-f]{40}")
MAX_AGE: Final = timedelta(seconds=60)
BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
LEDGER_SCHEMA_VERSION: Final = 2
TIMESTAMP_LENGTH: Final = 27
RUNTIME_SUBTREE: Final = "clinic-os-phase1a-runtime"
PRIVATE_FILE_MODE: Final = 0o600


def validate_authority_root(
    ledger: JsonObject,
    expected: JsonObject,
    phase: str,
) -> Path:
    if phase not in {"reserved", "active"}:
        raise ValueError
    if set(expected) != EXPECTED_KEYS or set(ledger) != ROOT_KEYS:
        raise ValueError
    attempt_id = text(expected.get("attempt_id"))
    foundation_sha = text(expected.get("foundation_sha"))
    worktree = text(expected.get("worktree_realpath"))
    if (
        ledger.get("schema_version") != LEDGER_SCHEMA_VERSION
        or isinstance(ledger.get("schema_version"), bool)
        or ledger.get("state") != "open"
        or ledger.get("closed_at_utc") is not None
        or ledger.get("rejection_close") is not None
        or ledger.get("attempt_id") != attempt_id
        or ledger.get("foundation_sha") != foundation_sha
        or ledger.get("worktree_realpath") != worktree
        or ledger.get("boot_id") != BOOT_ID_PATH.read_text(encoding="ascii").strip()
        or SHA40.fullmatch(foundation_sha) is None
    ):
        raise ValueError
    require_fresh(ledger.get("last_verified_at_utc"))
    _canonical_directory(Path(worktree))
    attempt_root = Path(text(ledger.get("attempt_root")))
    _canonical_directory(attempt_root)
    lock_path = Path(text(ledger.get("lock_path")))
    if lock_path.name != LOCK_NAME:
        raise ValueError
    _canonical_directory(lock_path.parent)
    _lock_file(lock_path)
    if attempt_root != lock_path.parent / RUNTIME_SUBTREE / attempt_id:
        raise ValueError
    if not isinstance(ledger.get("claims"), list):
        raise TypeError
    baseline = object_value(ledger.get("baseline"))
    if set(baseline) != {
        "containers",
        "evidence_lstat",
        "listeners",
        "networks",
        "omo_lstat",
        "shared_evidence_manifest",
        "volumes",
    }:
        raise ValueError
    for name in ("containers", "listeners", "networks", "volumes"):
        object_array(baseline.get(name))
    return attempt_root


def require_fresh(value: JsonValue) -> datetime:
    parsed = parse_timestamp(value)
    now = datetime.now(UTC)
    if not now - MAX_AGE <= parsed <= now:
        raise ValueError
    return parsed


def parse_timestamp(value: JsonValue) -> datetime:
    timestamp = text(value)
    if len(timestamp) != TIMESTAMP_LENGTH or not timestamp.endswith("Z"):
        raise ValueError
    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != timestamp:
        raise ValueError
    return parsed


def object_value(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError
    return value


def object_array(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        raise TypeError
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError
        result.append(item)
    return result


def string_array(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError
    return [item for item in value if isinstance(item, str)]


def text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError
    return value


def _canonical_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError
    identity = path.stat(follow_symlinks=False)
    if (
        not path.is_dir()
        or identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
    ):
        raise ValueError


def _lock_file(path: Path) -> None:
    identity = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(identity.st_mode)
        or stat.S_IMODE(identity.st_mode) != PRIVATE_FILE_MODE
        or identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
    ):
        raise ValueError
