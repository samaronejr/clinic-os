"""Private-descriptor bridge that hands one live TOTP code to a browser suite.

The code never reaches argv, an environment variable, a file, or a log. The
committed `totp_test_driver` writes exactly six bytes to a private pipe that
this module reads and immediately closes.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

DRIVER_MODULE: Final = "ops.testing.totp_test_driver"
CODE_LENGTH: Final = 6
CONFIRMED: Final = "confirmed-next-counter"
ANY_COUNTER: Final = -1


class BrowserTotpError(RuntimeError):
    """Reject a failed or malformed private-descriptor code handoff."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying handoff failure."""
        super().__init__(f"browser totp bridge rejected: {reason}")


def _fail(reason: str) -> Never:
    raise BrowserTotpError(reason)


@dataclass(frozen=True, slots=True)
class TotpContext:
    """Exact synthetic device context one helper process is allowed to read."""

    dsn: str
    tenant_id: str
    user_id: str

    def frame(self) -> bytes:
        """Return the single bounded canonical context frame."""
        return json.dumps(
            {
                "dsn": self.dsn,
                "last_t": ANY_COUNTER,
                "mode": CONFIRMED,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
            },
            sort_keys=True,
        ).encode()


def confirmed_code(context: TotpContext) -> str:
    """Run one helper process and return its six-byte confirmed device code."""
    interpreter = Path(sys.executable)
    if not interpreter.is_absolute() or not os.access(interpreter, os.X_OK):
        _fail("helper interpreter is not an executable absolute path")
    context_read, context_write = os.pipe()
    code_read, code_write = os.pipe()
    try:
        os.write(context_write, context.frame())
        os.close(context_write)
        completed = subprocess.run(  # noqa: S603 - fixed interpreter, closed argv.
            [
                str(interpreter),
                "-m",
                DRIVER_MODULE,
                "--mode",
                CONFIRMED,
                "--context-fd",
                str(context_read),
                "--code-fd",
                str(code_write),
            ],
            check=False,
            pass_fds=(context_read, code_write),
            capture_output=True,
        )
        os.close(code_write)
        code = os.read(code_read, CODE_LENGTH + 1)
    finally:
        for descriptor in (context_read, code_read):
            with contextlib.suppress(OSError):
                os.close(descriptor)
    if completed.returncode != 0 or completed.stdout or completed.stderr:
        _fail("helper process failed or emitted diagnostic bytes")
    if len(code) != CODE_LENGTH or not code.decode("ascii").isdigit():
        _fail("helper did not emit exactly six code bytes")
    return code.decode("ascii")
