"""Validate authored image identity and runtime filesystem confinement."""

from __future__ import annotations

import re
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

IMAGE_KEYS: Final = frozenset(
    {
        "available_suite_ids",
        "kind",
        "revision_sha",
        "source_entry_count",
        "source_manifest_sha256",
        "tree_sha",
    }
)
FILESYSTEM_KEYS: Final = frozenset(
    {"ipc_mode", "root_read_only", "shm_size_bytes", "tmpfs_mounts", "writable_paths"}
)
TMPFS_KEYS: Final = frozenset(
    {"gid", "mode", "nodev", "noexec", "nosuid", "size_bytes", "target", "uid"}
)
SHA40: Final = re.compile(r"[0-9a-f]{40}")
SHA256: Final = re.compile(r"[0-9a-f]{64}")
KNOWN_SUITES: Final = frozenset(
    {"availability", "patient", "runtime-https", "scheduling"}
)


def validate_service_contracts(image: JsonValue, filesystem: JsonValue) -> None:
    """Accept null legacy fields or exact authored contracts without widening."""
    if image is not None:
        _validate_image(_object(image, "image contract"))
    if filesystem is not None:
        _validate_filesystem(_object(filesystem, "filesystem contract"))


def _validate_image(value: JsonObject) -> None:
    suites = _strings(value.get("available_suite_ids"), "available suites")
    count = value.get("source_entry_count")
    kind = value.get("kind")
    if (
        set(value) != IMAGE_KEYS
        or kind not in {"application", "browser-runner"}
        or not _sha(value.get("revision_sha"), SHA40)
        or not _sha(value.get("tree_sha"), SHA40)
        or not _sha(value.get("source_manifest_sha256"), SHA256)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or suites != sorted(set(suites))
        or not set(suites) <= KNOWN_SUITES
        or (kind == "application" and suites != [])
    ):
        _fail("authored image contract is invalid")


def _validate_filesystem(value: JsonObject) -> None:
    mounts = _objects(value.get("tmpfs_mounts"), "tmpfs mounts")
    writable = _strings(value.get("writable_paths"), "writable paths")
    targets: list[str] = []
    for mount in mounts:
        target = mount.get("target")
        if set(mount) != TMPFS_KEYS or not isinstance(target, str):
            _fail("authored filesystem contract is invalid")
        targets.append(target)
        for key in ("gid", "mode", "size_bytes", "uid"):
            item = mount.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                _fail("authored filesystem contract is invalid")
        boolean_keys = ("nodev", "noexec", "nosuid")
        if any(not isinstance(mount.get(key), bool) for key in boolean_keys):
            _fail("authored filesystem contract is invalid")
    if (
        set(value) != FILESYSTEM_KEYS
        or value.get("root_read_only") is not True
        or value.get("ipc_mode") not in {"none", "private"}
        or not isinstance(value.get("shm_size_bytes"), int)
        or isinstance(value.get("shm_size_bytes"), bool)
        or targets != sorted(set(targets))
        or writable != targets
    ):
        _fail("authored filesystem contract is invalid")


def _sha(value: JsonValue, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{context} must be a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail(f"{context} must be a string array")
        result.append(item)
    return result


def _fail(message: str) -> Never:
    raise IsolationError(message)
