"""Control clean candidate browser-runner probes and fixture-only sessions."""

from __future__ import annotations

import asyncio
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Final, Never

import rfc8785

from ops.testing.browser_runner_contract import selected_suites
from ops.testing.browser_runner_probe import probe_runner_image
from ops.testing.image_smoke_controller import build_candidate, smoke_candidate
from ops.testing.isolation_common import JsonObject, JsonValue, load_json

MINIMUM_ARGUMENTS: Final = 2
HTTPS_ARGUMENTS: Final = 6
COMMAND_TIMEOUT_SECONDS: Final = 30


def main() -> None:
    """Dispatch probe or fixture-only session after the shell validates grammar."""
    if len(sys.argv) < MINIMUM_ARGUMENTS:
        _fail()
    operation = sys.argv[1]
    if operation == "probe":
        required = _required_suites(sys.argv[2:])
        _probe(Path.cwd(), required)
        return
    if operation == "session":
        _session_fixture(Path.cwd(), sys.argv[2:])
        return
    _fail()


def _probe(repository: Path, required: list[str]) -> None:
    revision = _git(repository, "rev-parse", "HEAD")
    try:
        image_id = smoke_candidate(repository, revision, "browser-runner", required)
    except FileNotFoundError:
        image_id = build_candidate(repository, revision, "browser-runner", required)
    contract = _runner_contract(repository, revision)
    selected_suites(_strings(contract.get("available_suite_ids")), required)
    probe_runner_image(repository, image_id, contract)


def _session_fixture(repository: Path, arguments: list[str]) -> None:
    required_start = 4
    if len(arguments) < required_start or arguments[0] != "--profile":
        _fail()
    profile = arguments[1]
    if arguments[2] != "--origin":
        _fail()
    origin = arguments[3]
    offset = required_start
    ca = None
    if profile == "container-https":
        if len(arguments) < HTTPS_ARGUMENTS or arguments[4] != "--ca":
            _fail()
        ca = arguments[5]
        offset = 6
    required = _required_suites(arguments[offset:])
    revision = _git(repository, "rev-parse", "HEAD")
    with redirect_stdout(io.StringIO()):
        smoke_candidate(repository, revision, "browser-runner", required)
    contract = _runner_contract(repository, revision)
    selected_suites(_strings(contract.get("available_suite_ids")), required)
    output = {
        "ca": ca,
        "origin": origin,
        "profile": profile,
        "required_suite_ids": required,
        "schema_version": 1,
    }
    sys.stdout.buffer.write(rfc8785.dumps(output) + b"\n")


def _runner_contract(repository: Path, revision: str) -> JsonObject:
    ledger, _ = load_json(repository / ".omo/evidence/isolation-ledger-phase1a.json")
    attempt_root = Path(_text(ledger.get("attempt_root")))
    envelope, _ = load_json(
        attempt_root / "candidate-images" / revision / "browser-runner-envelope.json"
    )
    return _object(envelope.get("image_contract"))


def _required_suites(arguments: list[str]) -> list[str]:
    if len(arguments) % 2:
        _fail()
    result: list[str] = []
    for index in range(0, len(arguments), 2):
        if arguments[index] != "--require-suite":
            _fail()
        result.append(arguments[index + 1])
    return result


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail()
    return value


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        _fail()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail()
        result.append(item)
    return result


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail()
    return value


def _git(repository: Path, *arguments: str) -> str:
    return asyncio.run(_git_async(repository, arguments))


async def _git_async(repository: Path, arguments: tuple[str, ...]) -> str:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-C",
        str(repository),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(
        process.communicate(),
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if process.returncode != 0:
        _fail()
    return stdout.decode("utf-8").strip()


def _fail() -> Never:
    raise _BrowserRunnerError


class _BrowserRunnerError(RuntimeError):
    pass


if __name__ == "__main__":
    main()
