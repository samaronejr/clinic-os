"""Freeze the exact all-green pre-F4 terminal inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Never

from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    regular_identity,
)
from ops.testing.isolation_terminal_publisher_authorizations import (
    validate_terminal_publisher_claim,
)

FINAL_AUTHORIZATION_COUNT = 2


def freeze_pre_f4(inputs: Path, terminal: Path, output: Path) -> bytes:
    """Require F1-F3 approval plus F4 precheck before publishing inventory."""
    regular_identity(inputs, mode=MODE_IMMUTABLE)
    manifest, _ = load_json(inputs)
    names = _publisher_paths(manifest, terminal)
    entries: list[JsonValue] = []
    for name in names:
        path = terminal / name
        regular_identity(path, mode=MODE_IMMUTABLE)
        raw = path.read_bytes()
        if name in {
            "F1-verdict.json",
            "F2-verdict.json",
            "F3/manifest.json",
            "F4-pre.txt",
        }:
            _approval(name, raw)
        entries.append(
            {"relative_path": name, "sha256": hashlib.sha256(raw).hexdigest()}
        )
    value: JsonObject = {
        "inputs_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
        "pre_f4": entries,
        "schema_version": 1,
        "sha": manifest["sha"],
        "tree_sha": manifest["tree_sha"],
    }
    raw = canonical_bytes(value)
    _write(output, raw)
    return raw


def _publisher_paths(manifest: JsonObject, terminal: Path) -> tuple[str, ...]:
    runtime = _object(manifest.get("runtime_bindings"), "runtime bindings")
    ledger_path = _absolute(runtime.get("ledger_path"), "ledger path")
    ledger, _ = load_json(ledger_path)
    publisher_id = manifest.get("terminal_publisher_claim_id")
    matches = [
        item
        for item in claim_objects(ledger["claims"])
        if item.get("claim_id") == publisher_id
    ]
    if len(matches) != 1:
        _fail("pre-F4 publisher is missing or duplicated")
    publisher = matches[0]
    _ = validate_terminal_publisher_claim(publisher, terminal)
    observed = _object(publisher.get("observed"), "publisher observation")
    outputs = _objects(observed.get("published_outputs"), "publisher outputs")
    if (
        len(outputs) < FINAL_AUTHORIZATION_COUNT
        or outputs[-2].get("authorization_id") != "f4-pre"
    ):
        _fail("pre-F4 authorization vector is incomplete")
    if (
        outputs[-2].get("status") != "published"
        or outputs[-1].get("status") != "unpublished"
    ):
        _fail("pre-F4 publisher prefix is not exact")
    if any(item.get("status") != "published" for item in outputs[:-1]):
        _fail("pre-F4 publisher has an unpublished predecessor")
    paths: list[str] = []
    for output_record in outputs[:-1]:
        for entry in _objects(output_record.get("entries"), "published entries"):
            relative = entry.get("relative_path")
            if not isinstance(relative, str):
                _fail("published entry path is invalid")
            paths.append(relative)
    expected = tuple(sorted(paths))
    actual = tuple(
        sorted(
            str(path.relative_to(terminal))
            for path in terminal.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    )
    if actual != expected:
        _fail("terminal tree differs from the exact PRE_F4 publisher prefix")
    return expected


def _approval(name: str, raw: bytes) -> None:
    if name == "F4-pre.txt":
        if raw != b"APPROVE-PRECHECK\n":
            _fail("F4 precheck did not approve")
        return
    value, _ = load_json_bytes(raw)
    if value.get("verdict", value.get("decision")) != "APPROVE":
        _fail("pre-F4 lane did not approve")


def load_json_bytes(raw: bytes) -> tuple[JsonObject, bytes]:
    """Parse one canonical object without granting a filesystem path."""
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        _fail("pre-F4 lane bytes are not canonical")
    return value, raw


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} is not an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} is not an object array")
    return [item for item in value if isinstance(item, dict)]


def _absolute(value: JsonValue, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is not absolute")
    return Path(value)


def _write(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, MODE_IMMUTABLE
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def main() -> int:
    """Dispatch the closed pre-F4 freezer form."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--terminal", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        _ = freeze_pre_f4(arguments.inputs, arguments.terminal, arguments.output)
    except (IsolationError, OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"freeze-pre-f4: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
