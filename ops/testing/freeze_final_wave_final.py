"""Freeze final.json only after publisher-created F4 approval."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Never

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

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


def freeze_final(inputs: Path, pre_f4: Path, f4: Path, output: Path) -> bytes:
    """Create the final control seal only for literal immutable F4 approval."""
    control = inputs.parents[1]
    if (
        inputs != control / "terminal/inputs.json"
        or pre_f4 != control / "pre-f4.json"
        or f4 != control / "terminal/F4-final.txt"
        or output != control / "final.json"
    ):
        _fail("final freezer paths differ from fixed control authorities")
    for path in (inputs, pre_f4, f4):
        regular_identity(path, mode=MODE_IMMUTABLE)
    if f4.read_bytes() != b"APPROVE\n":
        message = "F4 did not approve final freezing"
        raise RuntimeError(message)
    manifest, _ = load_json(inputs)
    _validate_f4_publisher(manifest, f4)
    value: JsonObject = {
        "approvals_sha256": hashlib.sha256(
            pre_f4.read_bytes() + f4.read_bytes()
        ).hexdigest(),
        "f4_sha256": hashlib.sha256(f4.read_bytes()).hexdigest(),
        "inputs_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
        "pre_f4_sha256": hashlib.sha256(pre_f4.read_bytes()).hexdigest(),
        "schema_version": 1,
        "sha": manifest["sha"],
        "tree_sha": manifest["tree_sha"],
    }
    raw = canonical_bytes(value)
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, MODE_IMMUTABLE
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw


def _validate_f4_publisher(manifest: JsonObject, f4: Path) -> None:
    runtime = _object(manifest.get("runtime_bindings"), "final runtime bindings")
    ledger_path = _absolute(runtime.get("ledger_path"), "final ledger path")
    publisher_id = manifest.get("terminal_publisher_claim_id")
    ledger, _ = load_json(ledger_path)
    matches = [
        item
        for item in claim_objects(ledger["claims"])
        if item.get("claim_id") == publisher_id
    ]
    if len(matches) != 1:
        _fail("final publisher is missing or duplicated")
    publisher = matches[0]
    _ = validate_terminal_publisher_claim(publisher, f4.parent)
    observed = _object(publisher.get("observed"), "final publisher observation")
    outputs = _objects(observed.get("published_outputs"), "final publisher outputs")
    if not outputs:
        _fail("final publisher output vector is absent")
    final = outputs[-1]
    if final.get("authorization_id") != "f4-final":
        _fail("final publisher authorization is invalid")
    if final.get("status") != "published":
        _fail("final publisher did not create F4")


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} is absent")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} are invalid")
    return [item for item in value if isinstance(item, dict)]


def _absolute(value: JsonValue, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is invalid")
    return Path(value)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def main() -> int:
    """Dispatch the closed final.json freezer form."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--pre-f4", required=True, type=Path)
    parser.add_argument("--f4", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        _ = freeze_final(
            arguments.inputs, arguments.pre_f4, arguments.f4, arguments.output
        )
    except (IsolationError, OSError, RuntimeError, ValueError) as error:
        sys.stderr.write(f"freeze-final: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
