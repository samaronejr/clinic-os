"""Parse the two F3 Python entrypoints without granting product authority."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable

PAIR_ARGUMENT_COUNT: Final = 2
SUPERVISOR_ARGUMENT_COUNT: Final = 6
MIN_PRIVATE_FD: Final = 3
SHA_HEX_LENGTH: Final = 40


def controller_entry(argv: list[str], stage: Callable[[str], object]) -> int:
    """Validate a controller form and reject unavailable runtime effects."""
    try:
        if len(argv) == PAIR_ARGUMENT_COUNT and argv[0] == "--stage":
            stage(argv[1])
            _fail("F3 child-stage runtime driver is unavailable")
        if len(argv) == PAIR_ARGUMENT_COUNT and argv[0] == "--control-fd":
            if int(argv[1]) < MIN_PRIVATE_FD:
                _fail("F3 controller descriptor rejected")
            _fail("F3 in-process runtime driver is unavailable")
        _fail("F3 controller argument vector rejected")
    except (RuntimeError, ValueError) as error:
        sys.stderr.write(f"final-e2e-controller: {error}\n")
        return 2


def supervisor_entry(argv: list[str], activate: Callable[[], None]) -> int:
    """Validate the outer form and reject an unavailable orchestration driver."""
    try:
        if (
            len(argv) != SUPERVISOR_ARGUMENT_COUNT
            or argv[0] != "--sha"
            or argv[2] != "--inputs"
            or argv[4] != "--terminal-evidence-dir"
        ):
            _fail("F3 supervisor argument vector rejected")
        sha, inputs, terminal = argv[1], Path(argv[3]), Path(argv[5])
        if (
            len(sha) != SHA_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in sha)
            or not inputs.is_absolute()
            or not terminal.is_absolute()
            or terminal != inputs.parent / "F3"
        ):
            _fail("F3 supervisor invocation rejected")
        activate()
        _fail("F3 orchestration runtime driver is unavailable")
    except (RuntimeError, OSError) as error:
        sys.stderr.write(f"final-e2e-supervisor: {error}\n")
        return 2


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)
