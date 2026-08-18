"""Bounded subprocess execution shared by contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured text output and status from one bounded child process."""

    returncode: int
    stdout: str
    stderr: str


def run_process(
    arguments: tuple[str, ...],
    timeout_seconds: float = 15,
) -> ProcessResult:
    """Execute a closed argument tuple and capture bounded-time output."""
    return asyncio.run(_run_process(arguments, timeout_seconds))


async def _run_process(
    arguments: tuple[str, ...],
    timeout_seconds: float,
) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=timeout_seconds,
    )
    return ProcessResult(
        process.returncode or 0,
        stdout.decode("utf-8"),
        stderr.decode("utf-8"),
    )
