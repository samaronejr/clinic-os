"""Refuse interpreters that lack the harness's native Linux pidfd APIs."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Final

CAPABILITIES: Final = (
    "os.pidfd_open",
    "os.waitid",
    "os.P_PIDFD",
    "signal.pidfd_send_signal",
)
RECORD_NAME: Final = "pidfd-preflight.json"
RECORD_SCHEMA: Final = 2


class PidfdUnavailableError(RuntimeError):
    """The selected Python build cannot provide the required pidfd APIs."""


def _interpreter() -> str:
    return f"Python {sys.version.split()[0]} ({Path(sys.executable).resolve()})"


def require_pidfd_support() -> dict[str, bool]:
    """Check for every required pidfd API before provisioning resources."""
    required = (
        (os, "pidfd_open"),
        (os, "waitid"),
        (os, "P_PIDFD"),
        (signal, "pidfd_send_signal"),
    )
    capabilities = {
        f"{module.__name__}.{name}": hasattr(module, name) for module, name in required
    }
    missing = [name for name, present in capabilities.items() if not present]
    if missing:
        message = (
            f"{_interpreter()} lacks required Linux pidfd APIs: {', '.join(missing)}. "
            "Select a pidfd-capable Python build with uv."
        )
        raise PidfdUnavailableError(message)
    return capabilities


def main() -> int:
    """Report whether the selected interpreter supplies the required APIs."""
    try:
        require_pidfd_support()
    except PidfdUnavailableError as error:
        sys.stderr.write(f"pidfd preflight: {error}\n")
        return 1
    sys.stdout.write(f"pidfd preflight passed: {_interpreter()}\n")
    return 0


def require_pidfd_at_startup() -> dict[str, bool]:
    """Exit before dispatch with one diagnostic if native pidfds are absent."""
    try:
        return require_pidfd_support()
    except PidfdUnavailableError as error:
        sys.stderr.write(f"pidfd preflight: {error}\n")
        raise SystemExit(1) from None


def write_hosted_record(
    directory: Path, slot: str, capabilities: dict[str, bool]
) -> None:
    """Publish the workload's startup check in its own hosted artifact."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    executable = Path(sys.executable).resolve(strict=True)
    with executable.open("rb") as stream:
        build_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / RECORD_NAME).open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "schema_version": RECORD_SCHEMA,
                "result": "PASS",
                "job": os.environ["GITHUB_JOB"],
                "slot": slot,
                "revision_sha": os.environ["GITHUB_SHA"],
                "run_id": os.environ["GITHUB_RUN_ID"],
                "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                "artifact": os.environ["CLINIC_CI_ARTIFACT"],
                "interpreter": str(executable),
                "version": sys.version.split()[0],
                "build_sha256": build_hash,
                "capabilities": capabilities,
            },
            stream,
            sort_keys=True,
        )
        stream.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
