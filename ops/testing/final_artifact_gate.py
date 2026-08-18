"""Stage one typed F4 decision without terminal publication authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    regular_identity,
)


def stage_decision(
    sha: str,
    inputs: Path,
    pre_f4: Path,
    decision: Path,
    outcome: Path,
) -> None:
    """Validate immutable handoff and stage only approval plus typed outcome."""
    regular_identity(inputs, mode=MODE_IMMUTABLE)
    regular_identity(pre_f4, mode=MODE_IMMUTABLE)
    manifest, _ = load_json(inputs)
    pre, _ = load_json(pre_f4)
    if manifest.get("sha") != sha or pre.get("sha") != sha:
        _fail("F4 handoff source identity differs")
    decision_raw = b"APPROVE\n"
    _write(decision, decision_raw)
    value: JsonObject = {
        "decision": "APPROVE",
        "decision_relative_path": "staging/F4-final.txt",
        "decision_sha256": hashlib.sha256(decision_raw).hexdigest(),
        "inputs_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
        "pre_f4_sha256": hashlib.sha256(pre_f4.read_bytes()).hexdigest(),
        "schema_version": 1,
        "sha": sha,
    }
    _write(outcome, canonical_bytes(value))


def _write(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        MODE_IMMUTABLE,
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--pre-f4", required=True, type=Path)
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--outcome", required=True, type=Path)
    return parser


def main() -> int:
    """Dispatch the fixed staged-decision form."""
    arguments = _parser().parse_args()
    try:
        stage_decision(
            arguments.sha,
            arguments.inputs,
            arguments.pre_f4,
            arguments.decision,
            arguments.outcome,
        )
    except (IsolationError, OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"final-artifact-gate: {error}\n")
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
