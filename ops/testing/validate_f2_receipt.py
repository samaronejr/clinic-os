"""Create and validate the exact fourteen-stage F2 success receipt."""

from __future__ import annotations

import argparse
import os
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
    JsonObject,
    JsonValue,
    canonical_bytes,
)

STAGE_COMMANDS: Final = (
    "timeout --signal=TERM --kill-after=120s 1800s make ci",
    'timeout 60s git diff --check cffbb1900ae2132560f20c27fcf1a514a1ef71aa.."$SHA"',
    "timeout 300s uv run ruff check .",
    "timeout 300s uv run ruff format --check .",
    "timeout 600s uv run mypy .",
    "timeout 600s uv run pip-audit --local",
    "timeout 600s uv run pytest -q tests/infra/test_production_settings.py "
    "tests/infra/test_release_settings.py tests/browser/test_browser_settings.py "
    "tests/infra/test_database_options.py tests/infra/test_data_mode.py "
    "tests/infra/test_product_telemetry.py tests/infra/test_timezone_source.py",
    "timeout 600s uv run pytest -q tests/infra/test_schema_policy.py "
    "tests/infra/test_resolver_catalog.py tests/infra/test_readiness.py "
    "tests/isolation/test_container_contract.py",
    "timeout 900s uv run pytest -q tests/patients/test_patient_services.py "
    "tests/scheduling/test_availability_concurrency.py "
    "tests/scheduling/test_appointment_concurrency.py "
    "tests/scheduling/test_appointment_transition_concurrency.py "
    "tests/infra/test_lifecycle_lock_races.py",
    "timeout 900s uv run pytest -q tests/audit/test_phase1_audit_contract.py "
    "tests/audit/test_phase1_audit_migration.py tests/audit/test_audit_append.py "
    "tests/audit/test_audit_unicode_boundaries.py",
    "timeout 300s uv run python ops/testing/assert_foundation_history.py "
    "cffbb1900ae2132560f20c27fcf1a514a1ef71aa",
    "timeout --signal=TERM --kill-after=120s 900s "
    'ops/testing/image_contract_gate.sh --sha "$SHA" --inputs '
    ".omo/evidence/clinic-os-phase1a-final/terminal/inputs.json",
    "timeout --signal=TERM --kill-after=120s 600s ops/testing/tls_stack.sh smoke",
    "timeout --signal=TERM --kill-after=120s 600s uv run pytest -q "
    "tests/infra/test_isolated_db_harness.py tests/isolation/test_isolation_ledger.py",
)
ROOT_KEYS: Final = {"schema_version", "sha", "tree_sha", "stages"}
STAGE_KEYS: Final = {
    "index",
    "argv",
    "started_at_utc",
    "ended_at_utc",
    "exit_code",
    "stdout_sha256",
    "stderr_sha256",
}
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


def validate_f2_receipt(receipt: JsonObject, sha: str, tree: str) -> bytes:
    """Return canonical bytes only for all fourteen exact green stages."""
    if set(receipt) != ROOT_KEYS or receipt.get("schema_version") != 1:
        _fail("F2 receipt has an open root or wrong version")
    if receipt.get("sha") != sha or receipt.get("tree_sha") != tree:
        _fail("F2 receipt candidate identity differs")
    if SHA40.fullmatch(sha) is None or SHA40.fullmatch(tree) is None:
        _fail("F2 receipt Git identity is invalid")
    stages = receipt.get("stages")
    if not isinstance(stages, list) or len(stages) != len(STAGE_COMMANDS):
        _fail("F2 receipt does not contain fourteen stages")
    for index, stage in enumerate(stages, 1):
        _validate_stage(stage, index)
    return canonical_bytes(receipt)


def _validate_stage(value: JsonValue, index: int) -> None:
    if not isinstance(value, dict) or set(value) != STAGE_KEYS:
        _fail("F2 stage has an open root")
    if value.get("index") != index or value.get("argv") != STAGE_COMMANDS[index - 1]:
        _fail("F2 stage order or command differs")
    if value.get("exit_code") != 0:
        _fail("F2 success receipt contains a failed stage")
    start, end = value.get("started_at_utc"), value.get("ended_at_utc")
    if (
        not isinstance(start, str)
        or not isinstance(end, str)
        or TIMESTAMP.fullmatch(start) is None
        or TIMESTAMP.fullmatch(end) is None
        or start > end
    ):
        _fail("F2 stage chronology is invalid")
    for key in ("stdout_sha256", "stderr_sha256"):
        digest = value.get(key)
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            _fail("F2 stage output digest is invalid")


def create_receipt(records: Path, sha: str, tree: str) -> JsonObject:
    """Load the bounded shell records into one closed success receipt."""
    stages: list[JsonValue] = []
    for index, command in enumerate(STAGE_COMMANDS, 1):
        root = records / str(index)
        stage: JsonObject = {
            "argv": _line(root / "argv"),
            "ended_at_utc": _line(root / "ended"),
            "exit_code": int(_line(root / "exit")),
            "index": index,
            "started_at_utc": _line(root / "started"),
            "stderr_sha256": _line(root / "stderr.sha256"),
            "stdout_sha256": _line(root / "stdout.sha256"),
        }
        stages.append(stage)
        if stage["argv"] != command:
            _fail("F2 shell stage command differs")
    return {"schema_version": 1, "sha": sha, "tree_sha": tree, "stages": stages}


def _line(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        _fail("F2 stage record is missing or linked")
    raw = path.read_text(encoding="utf-8")
    if not raw.endswith("\n") or "\n" in raw[:-1]:
        _fail("F2 stage record is not one LF-terminated line")
    return raw[:-1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("create", "validate"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    return parser


def main() -> int:
    """Create no-replace evidence or validate an existing immutable receipt."""
    arguments = _parser().parse_args()
    try:
        if arguments.operation == "create":
            if arguments.records is None:
                _fail("F2 create requires stage records")
            receipt = create_receipt(arguments.records, arguments.sha, arguments.tree)
            raw = validate_f2_receipt(receipt, arguments.sha, arguments.tree)
            descriptor = os.open(
                arguments.evidence,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            value: JsonValue = __import__("json").loads(
                arguments.evidence.read_text(encoding="utf-8")
            )
            if not isinstance(value, dict):
                _fail("F2 receipt is not an object")
            if (
                validate_f2_receipt(value, arguments.sha, arguments.tree)
                != arguments.evidence.read_bytes()
            ):
                _fail("F2 receipt bytes are not canonical")
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"f2-receipt: {error}\n")
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
