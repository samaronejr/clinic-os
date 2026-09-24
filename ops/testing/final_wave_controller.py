"""Parse and execute the four closed durable final-wave forms."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Never

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_final_wave_cli import (
    FinalWaveInvocation,
    Form,
    child_program,
)
from ops.testing.isolation_final_wave_runtime import run_final_wave_invocation

SHA_LENGTH = 40
MINIMUM_ARGUMENTS = 3
CONTROL = ".omo/evidence/clinic-os-phase1a-final"
TERMINAL = f"{CONTROL}/terminal"
INPUTS = f"{TERMINAL}/inputs.json"


def parse_invocation(argv: list[str]) -> FinalWaveInvocation:
    """Reject aliases, mixed forms, unknown flags, and caller destinations."""
    if not argv or argv[0] not in {"inputs", "scope-pre", "pre-f4", "final"}:
        _fail("invalid final-wave form")
    raw_form = argv[0]
    form: Form = (
        "inputs"
        if raw_form == "inputs"
        else "scope-pre"
        if raw_form == "scope-pre"
        else "pre-f4"
        if raw_form == "pre-f4"
        else "final"
    )
    if len(argv) < MINIMUM_ARGUMENTS or argv[1] != "--sha":
        _fail("invalid final-wave argument vector")
    sha = argv[2]
    if len(sha) != SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in sha
    ):
        _fail("invalid final-wave SHA")
    expected = _expected(form, sha)
    if argv != expected:
        _fail("final-wave arguments differ from the closed form")
    return FinalWaveInvocation(form, sha, tuple(argv[3:]))


def main() -> int:
    """Dispatch one closed form through the durable outer controller."""
    try:
        return run_final_wave_invocation(parse_invocation(sys.argv[1:]))
    except (IsolationError, OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"final-wave-controller: {error}\n")
        return 2


def _expected(form: Form, sha: str) -> list[str]:
    forms = {
        "inputs": [
            "inputs",
            "--sha",
            sha,
            "--control-root",
            CONTROL,
            "--output",
            INPUTS,
        ],
        "scope-pre": [
            "scope-pre",
            "--sha",
            sha,
            "--inputs",
            INPUTS,
            "--control-root",
            CONTROL,
            "--output",
            f"{TERMINAL}/F4-pre.txt",
        ],
        "pre-f4": [
            "pre-f4",
            "--sha",
            sha,
            "--inputs",
            INPUTS,
            "--control-root",
            CONTROL,
            "--namespace",
            TERMINAL,
            "--output",
            f"{CONTROL}/pre-f4.json",
        ],
        "final": [
            "final",
            "--sha",
            sha,
            "--inputs",
            INPUTS,
            "--pre-f4",
            f"{CONTROL}/pre-f4.json",
            "--control-root",
            CONTROL,
        ],
    }
    return forms[form]


def _fail(message: str) -> Never:
    raise IsolationError(message)


__all__ = [
    "FinalWaveInvocation",
    "Form",
    "child_program",
    "main",
    "parse_invocation",
]


if __name__ == "__main__":
    raise SystemExit(main())
