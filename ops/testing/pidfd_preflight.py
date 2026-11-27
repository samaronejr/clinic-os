"""Refuse interpreters that lack the harness's native Linux pidfd APIs."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


class PidfdUnavailableError(RuntimeError):
    """The selected Python build cannot provide the required pidfd APIs."""


def _interpreter() -> str:
    return f"Python {sys.version.split()[0]} ({Path(sys.executable).resolve()})"


def require_pidfd_support() -> None:
    """Check for every required pidfd API before provisioning resources."""
    required = (
        (os, "pidfd_open"),
        (os, "waitid"),
        (os, "P_PIDFD"),
        (signal, "pidfd_send_signal"),
    )
    missing = [
        f"{module.__name__}.{name}"
        for module, name in required
        if not hasattr(module, name)
    ]
    if missing:
        message = (
            f"{_interpreter()} lacks required Linux pidfd APIs: {', '.join(missing)}. "
            "Select a pidfd-capable Python build with uv."
        )
        raise PidfdUnavailableError(message)


def main() -> int:
    """Report whether the selected interpreter supplies the required APIs."""
    try:
        require_pidfd_support()
    except PidfdUnavailableError as error:
        sys.stderr.write(f"pidfd preflight: {error}\n")
        return 1
    sys.stdout.write(f"pidfd preflight passed: {_interpreter()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
