"""Construct closed final-wave payloads, environments, and deterministic IDs."""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError, load_json, raw_sha256
from ops.testing.isolation_final_wave_cli import child_program
from ops.testing.isolation_final_wave_record import PreF4Evidence

if TYPE_CHECKING:
    from ops.testing.isolation_final_wave_cli import FinalWaveInvocation
    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs


def final_wave_payload(
    invocation: FinalWaveInvocation,
    control: Path,
    inputs: Path,
    staging: Path | None,
) -> tuple[str, ...]:
    """Return one exact allowlisted child payload for the current form/stage."""
    program = child_program(invocation.form)
    if invocation.form == "inputs":
        return _python(
            program,
            "--sha",
            invocation.sha,
            "--control-root",
            str(control),
            "--output",
            str(inputs),
        )
    if invocation.form == "scope-pre":
        if staging is None:
            _fail("scope-pre staging is absent")
        return (
            str(program),
            "--sha",
            invocation.sha,
            "--inputs",
            str(inputs),
            "--evidence",
            str(staging / "staging/F4-pre.txt"),
        )
    if invocation.form == "pre-f4":
        return _python(
            program,
            "--inputs",
            str(inputs),
            "--terminal",
            str(control / "terminal"),
            "--output",
            str(control / "pre-f4.json"),
        )
    if staging is not None:
        return (
            str(program),
            "--sha",
            invocation.sha,
            "--inputs",
            str(inputs),
            "--pre-f4",
            str(control / "pre-f4.json"),
            "--staging",
            str(staging / "staging"),
        )
    return _python(
        program.with_name("freeze_final_wave_final.py"),
        "--inputs",
        str(inputs),
        "--pre-f4",
        str(control / "pre-f4.json"),
        "--f4",
        str(control / "terminal/F4-final.txt"),
        "--output",
        str(control / "final.json"),
    )


def final_wave_environment(
    runtime: ReviewRuntimeInputs | None,
) -> dict[str, str]:
    """Return the closed child environment without inheriting ambient values."""
    if runtime is None:
        values = (shutil.which("uv"), shutil.which("codex"))
        paths = [str(Path(value).parent) for value in values if value is not None]
        environment_root = sys.prefix
    else:
        paths = [str(runtime.uv_path.parent), str(runtime.codex_path.parent)]
        environment_root = str(runtime.worktree / ".venv")
    return {
        "HOME": "/var/empty",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": ":".join((*paths, "/usr/bin", "/bin")),
        "UV_PROJECT_ENVIRONMENT": environment_root,
    }


def scope_claim_id(attempt_id: str) -> str:
    """Derive the recoverable scope-pre staging claim UUID."""
    return str(uuid.uuid5(uuid.UUID(attempt_id), "final-wave:scope-pre"))


def final_predecessor(
    runtime: ReviewRuntimeInputs | None, control: Path
) -> PreF4Evidence:
    """Load the successful F1-F3 and F4-pre evidence for downstream forms."""
    if runtime is None:
        _fail("final-wave predecessor runtime is absent")
    f1, _ = load_json(runtime.controller_root / "F1.json")
    f2, _ = load_json(runtime.controller_root / "F2.json")
    return PreF4Evidence(
        runtime.inputs_sha256,
        f1,
        f2,
        raw_sha256((control / "terminal/F3/manifest.json").read_bytes()),
        raw_sha256((control / "terminal/F4-pre.txt").read_bytes()),
    )


def _python(program: Path, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-P", "-B", str(program), *arguments)


def _fail(message: str) -> Never:
    raise IsolationError(message)
