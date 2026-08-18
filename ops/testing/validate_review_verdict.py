"""Validate the closed F1/F2 reviewer verdict object."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final, Never

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from ops.testing.isolation_common import (
    IsolationError,
    JsonValue,
    canonical_bytes,
)

ROOT_KEYS: Final = {"schema_version", "lane", "sha", "verdict", "findings"}
FINDING_KEYS: Final = {"severity", "evidence", "repair"}
SEVERITIES: Final = {"critical", "high", "medium", "low"}
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")


def validate_review_verdict(raw: bytes, lane: str, sha: str) -> bytes:
    """Return canonical bytes only for one exact lane-bound JSON verdict."""
    if lane not in {"F1", "F2"} or SHA40.fullmatch(sha) is None:
        _fail("review verdict request identity is invalid")
    try:
        value: JsonValue = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = "review verdict must contain exactly one JSON object"
        raise IsolationError(message) from error
    if not isinstance(value, dict) or set(value) != ROOT_KEYS:
        _fail("review verdict has an open or non-object root")
    if (
        value.get("schema_version") != 1
        or value.get("lane") != lane
        or value.get("sha") != sha
        or value.get("verdict") not in {"APPROVE", "REJECT"}
    ):
        _fail("review verdict identity or enum is invalid")
    findings = value.get("findings")
    if not isinstance(findings, list):
        _fail("review findings must be an array")
    for finding in findings:
        _validate_finding(finding)
    verdict = value["verdict"]
    if (verdict == "APPROVE" and findings) or (verdict == "REJECT" and not findings):
        _fail("review verdict and findings disagree")
    return canonical_bytes(value)


def _validate_finding(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != FINDING_KEYS:
        _fail("review finding has an open root")
    if value.get("severity") not in SEVERITIES:
        _fail("review finding severity is invalid")
    for key in ("evidence", "repair"):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip():
            _fail(f"review finding {key} is blank")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--lane", required=True, choices=("F1", "F2"))
    parser.add_argument("--sha", required=True)
    return parser


def main() -> int:
    """Validate one staged verdict file without replacing it."""
    arguments = _parser().parse_args()
    try:
        validated = validate_review_verdict(
            arguments.input.read_bytes(),
            arguments.lane,
            arguments.sha,
        )
        if arguments.input.read_bytes() != validated:
            _fail("staged review verdict is not canonical")
    except (IsolationError, OSError) as error:
        sys.stderr.write(f"review-verdict: {error}\n")
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
