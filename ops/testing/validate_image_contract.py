"""Validate released application and runner images without rebuilding them."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Final, Never

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from ops.testing.browser_runner_contract import filesystem_contract
from ops.testing.candidate_pair import validate_candidate_pair
from ops.testing.isolation_candidate_verification import verify_candidate_image
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.process_helpers import run_process

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
LEDGER: Final = PROJECT_ROOT / ".omo/evidence/isolation-ledger-phase1a.json"
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
DOCKER: Final = shutil.which("docker")
APPLICATION_ENTRYPOINT: Final = ["/app/ops/container/entrypoint.sh"]
APPLICATION_CMD: Final = [
    "gunicorn",
    "--config=/app/ops/container/gunicorn_no_proxy.py",
    "--bind=0.0.0.0:8000",
    "--workers=2",
    "--threads=4",
    "--timeout=30",
    "--graceful-timeout=30",
    "--keep-alive=5",
    "--max-requests=1000",
    "--max-requests-jitter=100",
    "--access-logfile=-",
    "--error-logfile=-",
    "config.wsgi:application",
]
RUNNER_ENTRYPOINT: Final = ["python", "-m", "ops.testing.browser_session"]
RUNNER_CMD: Final = ["hold"]


def validate_image_contract(inputs_path: Path, sha: str) -> None:
    """Re-authenticate candidate pairs, suite inputs, and immutable image config."""
    if SHA40.fullmatch(sha) is None:
        _fail("image gate SHA is invalid")
    regular_identity(inputs_path, mode=MODE_IMMUTABLE)
    inputs, input_raw = load_json(inputs_path)
    if inputs.get("schema_version") != 1 or inputs.get("sha") != sha:
        _fail("image gate final input identity differs")
    tree = _git("rev-parse", f"{sha}^{{tree}}")
    if (
        _git("rev-parse", "HEAD") != sha
        or _git("status", "--porcelain=v1", "--untracked-files=all")
        or inputs.get("tree_sha") != tree
    ):
        _fail("image gate requires the exact clean SHA and tree")
    ledger, _ = load_json(LEDGER)
    claims = _claims(ledger.get("claims"))
    application = _candidate(inputs.get("application_candidate"), claims)
    runner = _candidate(inputs.get("runner_candidate"), claims)
    _candidate_identity(application, sha, tree, "application")
    _candidate_identity(runner, sha, tree, "browser-runner")
    suites = _strings(inputs.get("final_required_browser_suites"), "final suites")
    runner_contract = _object(runner.get("image_contract"), "runner image contract")
    if (
        suites != sorted(set(suites))
        or runner_contract.get("available_suite_ids") != suites
    ):
        _fail("runner available suites differ from frozen final suites")
    _suite_file(inputs, suites)
    if inputs.get("ci_required_browser_suites_sha256") is not None:
        ci_path = PROJECT_ROOT / "ops/testing/ci-required-browser-suites.txt"
        if raw_sha256(ci_path.read_bytes()) != inputs.get(
            "ci_required_browser_suites_sha256"
        ):
            _fail("three-suite CI input hash drifted")
    if inputs.get("runner_filesystem_contract") != filesystem_contract():
        _fail("runner filesystem confinement differs")
    references = _strings(
        inputs.get("production_service_image_ids"), "production service image IDs"
    )
    if any(value != application["image_id"] for value in references):
        _fail("production service does not reuse the frozen application image")
    verify_candidate_image(ledger, {}, application)
    verify_candidate_image(ledger, {}, runner)
    _image_config(application["image_id"], APPLICATION_ENTRYPOINT, APPLICATION_CMD)
    _image_config(runner["image_id"], RUNNER_ENTRYPOINT, RUNNER_CMD)
    if raw_sha256(input_raw) != hashlib.sha256(input_raw).hexdigest():
        _fail("final input hash calculation failed")


def _candidate(value: JsonValue, claims: list[JsonObject]) -> JsonObject:
    if not isinstance(value, dict):
        _fail("candidate input is not an object")
    envelope_path = _absolute(value.get("envelope_path"), "candidate envelope")
    history_path = _absolute(value.get("history_path"), "candidate history")
    regular_identity(envelope_path, mode=MODE_IMMUTABLE)
    regular_identity(history_path, mode=MODE_IMMUTABLE)
    envelope_raw, history_raw = envelope_path.read_bytes(), history_path.read_bytes()
    if raw_sha256(envelope_raw) != value.get("envelope_sha256") or raw_sha256(
        history_raw
    ) != value.get("history_sha256"):
        _fail("candidate pair hash differs from frozen inputs")
    envelope = validate_candidate_pair(envelope_raw, history_raw, claims)
    if envelope.get("image_id") != value.get("image_id") or envelope.get(
        "image_contract"
    ) != value.get("image_contract"):
        _fail("candidate pair differs from frozen input projection")
    return envelope


def _candidate_identity(
    envelope: JsonObject,
    sha: str,
    tree: str,
    kind: str,
) -> None:
    contract = envelope.get("image_contract")
    if not isinstance(contract, dict) or (
        envelope.get("revision_sha"),
        envelope.get("tree_sha"),
        contract.get("kind"),
    ) != (sha, tree, kind):
        _fail("candidate envelope source identity differs")


def _suite_file(inputs: JsonObject, suites: list[str]) -> None:
    value = inputs.get("final_required_browser_suites_path")
    digest = inputs.get("final_required_browser_suites_sha256")
    path = _absolute(value, "final suite file")
    regular_identity(path, mode=MODE_IMMUTABLE)
    expected = "".join(f"{suite}\n" for suite in suites).encode()
    if path.read_bytes() != expected or raw_sha256(expected) != digest:
        _fail("final suite file bytes or hash differ")


def _image_config(
    image_id: JsonValue, entrypoint: list[str], command: list[str]
) -> None:
    if not isinstance(image_id, str) or DOCKER is None:
        _fail("Docker image inspection is unavailable")
    fields = (
        _docker(image_id, "{{json .Config.User}}"),
        _docker(image_id, "{{json .Config.Entrypoint}}"),
        _docker(image_id, "{{json .Config.Cmd}}"),
    )
    if fields != ("10001:10001", entrypoint, command):
        _fail("image UID, entrypoint, or command drifted")


def _docker(image_id: str, template: str) -> JsonValue:
    if DOCKER is None:
        _fail("Docker image inspection is unavailable")
    result = run_process((DOCKER, "image", "inspect", "--format", template, image_id))
    if result.returncode != 0:
        _fail("Docker image inspection failed")
    value: JsonValue = json.loads(result.stdout)
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} is not an object")
    return value


def _git(*arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        _fail("git executable is unavailable")
    result = run_process((executable, "-C", str(PROJECT_ROOT), *arguments))
    if result.returncode != 0:
        _fail("Git source inspection failed")
    return result.stdout.strip()


def _claims(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("ledger claims are invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("ledger claims are invalid")
        result.append(item)
    return result


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} are invalid")
    return [item for item in value if isinstance(item, str)]


def _absolute(value: JsonValue, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} path is invalid")
    return Path(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--inputs", required=True, type=Path)
    return parser


def main() -> int:
    """Validate stage-12 image inputs without creating Docker resources."""
    arguments = _parser().parse_args()
    try:
        validate_image_contract(arguments.inputs, arguments.sha)
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"image-contract-gate: {error}\n")
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
