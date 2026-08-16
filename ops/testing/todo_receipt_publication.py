"""Publish one validated todo receipt through its active filesystem claim."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    stable_lock,
    write_atomic_replace,
    write_no_replace,
)

LEDGER_RELATIVE: Final = Path(".omo/evidence/isolation-ledger-phase1a.json")


def publish_validated_receipt(
    worktree: Path,
    staged: Path,
    destination: Path,
    raw: bytes,
) -> None:
    """Authorize, no-replace publish, and record one immutable receipt."""
    ledger_path = worktree / LEDGER_RELATIVE
    ledger, _ = load_json(ledger_path)
    lock_path = Path(_text(ledger["lock_path"], "ledger lock path"))
    with stable_lock(lock_path, create=False) as (_, lock_identity):
        ledger, _ = load_json(ledger_path)
        if (
            ledger.get("lock_identity") != lock_identity
            or ledger.get("state") != "open"
        ):
            _fail("todo receipt requires the recorded open-ledger lock")
        claim, observed = _publication_authority(
            ledger,
            staged,
            destination,
            raw,
        )
        _publish_or_adopt(destination, raw)
        observed["entries"] = [_published_entry(destination, raw)]
        observed["status"] = "published"
        claim["last_verified_at_utc"] = ledger["last_verified_at_utc"]
        write_atomic_replace(ledger_path, canonical_bytes(ledger))


def _publication_authority(
    ledger: JsonObject,
    staged: Path,
    destination: Path,
    raw: bytes,
) -> tuple[JsonObject, JsonObject]:
    attempt_root = Path(_text(ledger["attempt_root"], "attempt root"))
    if (
        destination.parent != attempt_root / "todo-evidence"
        or not destination.name.startswith("task-")
        or not destination.name.endswith("-clinic-os-phase-1a-staff-scheduling.json")
    ):
        _fail("todo receipt destination is outside the current attempt allowlist")
    matches: list[tuple[JsonObject, JsonObject]] = []
    for claim in _objects(ledger["claims"], "claims"):
        if claim.get("kind") != "filesystem" or claim.get("status") != "active":
            continue
        claim_root = attempt_root / _text(
            claim["root_relative_path"],
            "claim root relative path",
        )
        if not _inside(staged, claim_root):
            continue
        desired = _object(claim["desired"], "filesystem desired")
        observed = _object(claim["observed"], "filesystem observed")
        _validate_staged_file(staged, claim_root, desired, raw)
        authorizations = _objects(desired["published_outputs"], "authorizations")
        observed_outputs = _objects(observed["published_outputs"], "observed outputs")
        for authorization in authorizations:
            if _authorization_matches(authorization, destination):
                output = _observed_authorization(
                    observed_outputs,
                    _text(authorization["authorization_id"], "authorization ID"),
                )
                matches.append((claim, output))
    if len(matches) != 1:
        _fail("todo receipt has no unique active publication authority")
    claim, observed = matches[0]
    if observed.get("status") not in {"unpublished", "published"}:
        _fail("todo receipt authorization has an invalid observed state")
    if observed.get("status") == "published" and not observed.get("entries"):
        _fail("published todo receipt authorization has no identity")
    return claim, observed


def _validate_staged_file(
    path: Path,
    root: Path,
    desired: JsonObject,
    raw: bytes,
) -> None:
    value = path.stat(follow_symlinks=False)
    relative = path.relative_to(root).as_posix()
    expected = [
        item
        for item in _objects(desired["owned_files"], "owned files")
        if item.get("relative_path") == relative
    ]
    if (
        len(expected) != 1
        or not stat.S_ISREG(value.st_mode)
        or stat.S_IMODE(value.st_mode) != MODE_PRIVATE
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_nlink != 1
        or expected[0].get("sha256") != raw_sha256(raw)
    ):
        _fail("staged todo receipt does not match its owned-file identity")


def _authorization_matches(value: JsonObject, destination: Path) -> bool:
    return (
        value.get("output_kind") == "todo-evidence"
        and value.get("governing_lock") == "stable"
        and value.get("root_path") == str(destination.parent)
        and value.get("relative_paths") == [destination.name]
        and value.get("mode") == MODE_IMMUTABLE
        and value.get("uid") == os.geteuid()
        and value.get("gid") == os.getegid()
    )


def _observed_authorization(values: list[JsonObject], identifier: str) -> JsonObject:
    matches = [value for value in values if value.get("authorization_id") == identifier]
    if len(matches) != 1:
        _fail("todo receipt observed authorization is not unique")
    return matches[0]


def _publish_or_adopt(path: Path, raw: bytes) -> None:
    try:
        write_no_replace(path, raw, mode=MODE_IMMUTABLE)
    except FileExistsError:
        value = path.stat(follow_symlinks=False)
        existing = path.read_bytes()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != MODE_IMMUTABLE
            or value.st_uid != os.geteuid()
            or value.st_gid != os.getegid()
            or value.st_nlink != 1
            or existing != raw
        ):
            _fail("existing todo receipt publication differs")


def _published_entry(path: Path, raw: bytes) -> JsonObject:
    value = path.stat(follow_symlinks=False)
    return {
        "gid": value.st_gid,
        "mode": stat.S_IMODE(value.st_mode),
        "relative_path": path.name,
        "sha256": raw_sha256(raw),
        "size_bytes": len(raw),
        "uid": value.st_uid,
    }


def _inside(path: Path, root: Path) -> bool:
    return path.is_absolute() and not path.is_symlink() and path.parent == root


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
