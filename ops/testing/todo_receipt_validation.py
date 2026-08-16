"""Validate committed TDD, acceptance, resource, and cleanup receipt evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Never, Protocol, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
)


class _GitReader(Protocol):
    def __call__(self, *arguments: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _CommitContext:
    commit: str
    changed_paths: frozenset[str]
    git: _GitReader


def _values(raw: str, separator: str = " ") -> frozenset[str]:
    return frozenset(raw.split(separator))


CLAIM_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
TDD_KEYS = frozenset({"test_paths", "test_source_sha256", "red", "green"})
RUN_KEYS = _values(
    "argv started_at_utc ended_at_utc exit_code classification "
    "expected_failed_nodeids observed_failed_nodeids stdout_sha256 stderr_sha256"
)
COMMAND_KEYS = _values(
    "argv started_at_utc ended_at_utc exit_code stdout_sha256 stderr_sha256"
)
TESTS_AFTER_KEYS = COMMAND_KEYS | _values(
    "test_paths test_source_sha256 classification http_contract_nodeids"
)
RESOURCE_KEYS = _values("claim_id kind activated_at_utc released_at_utc")
CLEANUP_KEYS = _values(
    "verified_at_utc claim_ids_absent paths_absent containers_absent "
    "volumes_absent networks_absent listeners_absent process_members_absent "
    "shared_evidence_manifest_sha256"
)
ABSENCE_KEYS = CLEANUP_KEYS - {
    "verified_at_utc",
    "shared_evidence_manifest_sha256",
}
FORBIDDEN_VALUES = _values(
    "postgresql://|secret_key=|password=|authorization: bearer|cookie:", "|"
)


def validate_committed_receipt_evidence(
    receipt: JsonObject,
    commit: str,
    git: _GitReader,
) -> None:
    """Validate evidence only after the caller authenticates this module."""
    changed = frozenset(
        git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
        .decode()
        .strip("\0")
        .split("\0")
    )
    entries = _objects(receipt["tdd_entries"], "TDD entries")
    if not entries:
        _fail("receipt has no TDD entry")
    context = _CommitContext(commit=commit, changed_paths=changed, git=git)
    green_nodeids: set[str] = set()
    for entry in entries:
        green_nodeids.update(_validate_tdd(entry, context))
    _validate_tests_after(receipt["tests_after_entries"], green_nodeids, context)
    _validate_evidence(receipt)
    _validate_redaction(receipt)


def _validate_tdd(
    entry: JsonObject,
    context: _CommitContext,
) -> list[str]:
    _exact(entry, TDD_KEYS, "TDD entry")
    paths = _test_paths(entry["test_paths"])
    if not set(paths).issubset(context.changed_paths):
        _fail("primary commit does not contain every TDD test path")
    if entry.get("test_source_sha256") != _test_source_sha256(context, paths):
        _fail("TDD test-source digest does not match committed blobs")
    red = _object(entry["red"], "red run")
    green = _object(entry["green"], "green run")
    _validate_command(red, RUN_KEYS, "red TDD")
    _validate_command(green, RUN_KEYS, "green TDD")
    expected = _nodeids(red["expected_failed_nodeids"], "red expected", paths)
    observed = _nodeids(red["observed_failed_nodeids"], "red observed", paths)
    green_expected = _nodeids(green["expected_failed_nodeids"], "green expected", paths)
    green_observed = _nodeids(green["observed_failed_nodeids"], "green observed", paths)
    if (
        red.get("exit_code") != 1
        or red.get("classification") != "expected-assertion-failure"
        or not expected
        or expected != observed
    ):
        _fail("red run failed node IDs do not match the expected assertions")
    if (
        green.get("exit_code") != 0
        or green.get("classification") != "pass"
        or green_expected != expected
        or green_observed
    ):
        _fail("green run does not prove the red target node IDs")
    red_end = red["ended_at_utc"]
    green_start = green["started_at_utc"]
    if not isinstance(red_end, str) or not isinstance(green_start, str):
        _fail("red and green chronology is not textual")
    if red["argv"] != green["argv"] or red_end > green_start:
        _fail("red and green runs do not preserve selector chronology")
    return expected


def _validate_tests_after(
    value: JsonValue,
    green_nodeids: set[str],
    context: _CommitContext,
) -> None:
    entries = _objects(value, "tests-after entries")
    for entry in entries:
        _validate_command(entry, TESTS_AFTER_KEYS, "tests-after")
        paths = _test_paths(entry["test_paths"])
        nodeids = _nodeids(
            entry["http_contract_nodeids"], "tests-after HTTP contract", paths
        )
        if (
            entry.get("classification") != "visual-polish-no-behavior"
            or entry.get("exit_code") != 0
            or entry.get("test_source_sha256") != _test_source_sha256(context, paths)
            or not nodeids
            or not set(nodeids).issubset(green_nodeids)
        ):
            _fail("tests-after entry lacks an already-green HTTP contract")


def _validate_evidence(receipt: JsonObject) -> None:
    acceptance = _objects(receipt["acceptance"], "acceptance")
    resources = _objects(receipt["resources"], "resources")
    cleanup = _object(receipt["cleanup"], "cleanup")
    for command in acceptance:
        _validate_command(command, COMMAND_KEYS, "acceptance")
        if command.get("exit_code") != 0:
            _fail("successful receipt contains failed acceptance evidence")
    claim_ids: list[str] = []
    for resource in resources:
        _exact(resource, RESOURCE_KEYS, "resource")
        claim_id = resource.get("claim_id")
        if not isinstance(claim_id, str) or CLAIM_ID.fullmatch(claim_id) is None:
            _fail("resource claim ID is not a canonical UUID")
        if resource.get("kind") not in {"stack", "process", "filesystem"}:
            _fail("resource kind is invalid")
        _timestamps(resource, "activated_at_utc", "released_at_utc")
        claim_ids.append(claim_id)
    if claim_ids != sorted(set(claim_ids)):
        _fail("resource claim IDs are not sorted and unique")
    _exact(cleanup, CLEANUP_KEYS, "cleanup")
    _timestamp(cleanup.get("verified_at_utc"), "cleanup verification")
    _hex(cleanup["shared_evidence_manifest_sha256"], 64, "shared evidence")
    if any(cleanup.get(key) is not True for key in ABSENCE_KEYS):
        _fail("cleanup absence proof is incomplete")


def _validate_command(value: JsonObject, keys: frozenset[str], context: str) -> None:
    _exact(value, keys, f"{context} command")
    _strings(value["argv"], f"{context} argv")
    _timestamps(value, "started_at_utc", "ended_at_utc")
    _hex(value["stdout_sha256"], 64, "stdout SHA-256")
    _hex(value["stderr_sha256"], 64, "stderr SHA-256")


def _test_paths(value: JsonValue) -> list[str]:
    paths = _strings(value, "test paths")
    if paths != sorted(set(paths)):
        _fail("test paths are not sorted and unique")
    for raw in paths:
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or not raw.startswith("tests/"):
            _fail("test path is outside the repository test root")
    return paths


def _test_source_sha256(context: _CommitContext, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        raw = context.git("show", f"{context.commit}:{path}")
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _timestamps(value: JsonObject, start_key: str, end_key: str) -> None:
    start = _timestamp(value.get(start_key), start_key)
    end = _timestamp(value.get(end_key), end_key)
    if start > end:
        _fail("evidence timestamp chronology is invalid")


def _timestamp(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        _fail(f"{context} must be a canonical UTC timestamp")
    return value


def _nodeids(value: JsonValue, context: str, paths: list[str]) -> list[str]:
    nodeids = _strings(value, f"{context} node IDs")
    if nodeids != sorted(set(nodeids)) or any(
        "::" not in nodeid or nodeid.split("::", 1)[0] not in paths
        for nodeid in nodeids
    ):
        _fail(f"{context} node IDs are not sorted repository test nodes")
    return nodeids


def _validate_redaction(value: JsonValue) -> None:
    if isinstance(value, str) and any(
        item in value.casefold() for item in FORBIDDEN_VALUES
    ):
        _fail("receipt contains a prohibited raw secret-bearing value")
    if isinstance(value, list):
        for item in value:
            _validate_redaction(item)
    if isinstance(value, dict):
        for item in value.values():
            _validate_redaction(item)


def _exact(value: JsonObject, keys: frozenset[str], context: str) -> None:
    if set(value) != keys:
        _fail(f"{context} has the wrong closed key set")


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        _fail(f"{context} must be a nonblank string array")
    return cast("list[str]", value)


def _hex(value: JsonValue, length: int, context: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None
    ):
        _fail(f"{context} must be lowercase {length}-hex")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
