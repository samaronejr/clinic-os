"""Own the strict argv and staged-file boundary for todo receipt publication."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
)
from ops.testing.todo_receipt_publication import publish_validated_receipt

if TYPE_CHECKING:
    from collections.abc import Callable

ARGUMENT_COUNT: Final = 6
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def run_cli(validator: Callable[[JsonObject, str], bytes]) -> int:
    """Validate exact argv and publish only validator-approved canonical bytes."""
    arguments = sys.argv[1:]
    if len(arguments) != ARGUMENT_COUNT or arguments[0::2] != [
        "--staged",
        "--destination",
        "--commit",
    ]:
        sys.stderr.write("publish-todo-receipt: invalid command grammar\n")
        return 2
    try:
        staged = Path(arguments[1])
        destination = Path(arguments[3])
        commit = arguments[5]
        value = staged.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != MODE_PRIVATE
        ):
            _fail("staged receipt must be a mode-0600 regular file")
        receipt, raw = load_json(staged)
        if validator(receipt, commit) != raw:
            _fail("staged receipt bytes changed during validation")
        publish_validated_receipt(PROJECT_ROOT, staged, destination, raw)
    except (
        IsolationError,
        FileExistsError,
        FileNotFoundError,
        PermissionError,
        OSError,
    ) as error:
        sys.stderr.write(f"publish-todo-receipt: {error}\n")
        return 2
    sys.stdout.write(f"{raw_sha256(raw)}\n")
    return 0


def _fail(message: str) -> None:
    raise IsolationError(message)
