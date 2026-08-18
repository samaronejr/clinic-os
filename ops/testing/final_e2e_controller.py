"""Own the exact bounded twelve-stage F3 product state machine."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.f3_cli import controller_entry


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Bind one closed stage name to its deadline and execution boundary."""

    name: str
    seconds: int
    execution: str


STAGES: Final = (
    StageSpec("preflight-and-claims", 120, "child"),
    StageSpec("tls-materializer-source-db", 600, "child"),
    StageSpec("owner-release-migrations", 900, "child"),
    StageSpec("owner-bootstrap", 300, "child"),
    StageSpec("source-https-and-runner-start", 300, "in-process"),
    StageSpec("owner-enrollment-start-ack-helper-confirm", 300, "in-process"),
    StageSpec(
        "three-owner-helpers-three-provisions-two-staff-enrollments",
        900,
        "in-process",
    ),
    StageSpec("browser-visual", 1800, "in-process"),
    StageSpec("audit-posture", 600, "child"),
    StageSpec("restore-rehearsal", 1800, "child"),
    StageSpec("restored-https-smoke-audit", 600, "child"),
    StageSpec("reverse-cleanup", 300, "child"),
)
CHILD_STAGES: Final = frozenset(
    item.name for item in STAGES if item.execution == "child"
)
IN_PROCESS_STAGES: Final = frozenset(
    item.name for item in STAGES if item.execution == "in-process"
)
SECRET_FRAMES: Final = (
    ("owner-start", "password"),
    ("owner-confirm", "code"),
    ("clinic-admin-start", "password"),
    ("clinic-admin-confirm", "code"),
    ("physician-start", "password"),
    ("physician-confirm", "code"),
    ("receptionist-login", "password"),
)
TOTP_HELPERS: Final = (
    "owner-pending-enrollment",
    "owner-confirmed-clinic-admin-provision",
    "owner-confirmed-receptionist-provision",
    "owner-confirmed-physician-provision",
    "clinic-admin-pending-enrollment",
    "physician-pending-enrollment",
)
GIT_SHA_HEX_LENGTH: Final = 40


class StageDeadlineError(RuntimeError):
    """Report one typed in-process stage deadline."""

    def __init__(self, name: str) -> None:
        """Retain the timed-out stage name for typed error handling."""
        super().__init__(f"F3 stage deadline expired: {name}")
        self.name = name


StageDeadline = StageDeadlineError


class StageContractError(RuntimeError):
    """Reject an execution boundary not present in the closed stage machine."""


def _fail(reason: str) -> Never:
    raise StageContractError(reason)


def _deadline(name: str) -> StageDeadlineError:
    return StageDeadlineError(name)


class _StageEffects(Protocol):
    """Expose product effects without granting the controller process authority."""

    def child_stage(self, name: str, seconds: int, argv: tuple[str, ...]) -> None: ...

    def start_source_session(self) -> object: ...

    def continue_source_session(self, name: str, session: object) -> None: ...


StageEffects = _StageEffects


def run_in_process_stage[T](name: str, seconds: int, operation: Callable[[], T]) -> T:
    """Run stages 5-8 in this main process under a real-time signal deadline."""
    if name not in IN_PROCESS_STAGES or seconds != _stage(name).seconds:
        _fail("in-process stage contract rejected")
    started = time.monotonic()
    prior_handler = signal.getsignal(signal.SIGALRM)
    prior_delay, prior_interval = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signal_number: int, _frame: object) -> None:
        raise _deadline(name)

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        result = operation()
        if time.monotonic() - started > seconds:
            raise _deadline(name)
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)
        if prior_delay > 0:
            remaining = max(0.000001, prior_delay - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, prior_interval)


def run_child_stage(
    name: str,
    seconds: int,
    argv: tuple[str, ...],
    request: Callable[[str, int, tuple[str, ...]], None],
) -> None:
    """Request only stages 1-4 and 9-12 through the supervisor boundary."""
    if (
        name not in CHILD_STAGES
        or seconds != _stage(name).seconds
        or argv != ("stage", name)
    ):
        _fail("child stage contract rejected")
    request(name, seconds, argv)


def run_state_machine(effects: StageEffects) -> object:
    """Execute all stages in order while retaining the one stage-5 session."""
    session: object | None = None
    for stage in STAGES:
        if stage.execution == "child":
            run_child_stage(
                stage.name,
                stage.seconds,
                ("stage", stage.name),
                effects.child_stage,
            )
        elif stage.name == "source-https-and-runner-start":
            session = run_in_process_stage(
                stage.name,
                stage.seconds,
                effects.start_source_session,
            )
        else:
            if session is None:
                _fail("source browser session is absent")
            retained = session
            run_in_process_stage(
                stage.name,
                stage.seconds,
                partial(effects.continue_source_session, stage.name, retained),
            )
    if session is None:
        _fail("source browser session is absent")
    return session


def require_exact_checkout(repository: Path, sha: str, worktree: Path) -> str:
    """Reject a dirty, substituted, wrong-SHA, wrong-tree, or wrong-root checkout."""
    if len(sha) != GIT_SHA_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in sha
    ):
        _fail("F3 checkout SHA rejected")
    root = _git_text(repository, "rev-parse", "--show-toplevel")
    head = _git_text(repository, "rev-parse", "HEAD")
    expected_tree = _git_text(repository, "rev-parse", f"{sha}^{{tree}}")
    observed_tree = _git_text(repository, "rev-parse", "HEAD^{tree}")
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if (
        Path(root).resolve(strict=True) != worktree.resolve(strict=True)
        or head != sha
        or observed_tree != expected_tree
        or status
    ):
        _fail("F3 exact-SHA clean checkout rejected")
    return observed_tree


def _stage(name: str) -> StageSpec:
    matches = [item for item in STAGES if item.name == name]
    if len(matches) != 1:
        _fail("unknown F3 stage")
    return matches[0]


def _git_text(repository: Path, *arguments: str) -> str:
    return _git(repository, *arguments).decode("ascii").strip()


def _git(repository: Path, *arguments: str) -> bytes:
    return asyncio.run(_git_async(repository, arguments))


async def _git_async(repository: Path, arguments: tuple[str, ...]) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-C",
        str(repository),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"HOME": "/nonexistent", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        _fail("F3 Git observation failed")
    return stdout


def main(argv: list[str] | None = None) -> int:
    """Reject any controller form until its supervisor-owned driver is available."""
    values = sys.argv[1:] if argv is None else argv
    return controller_entry(values, _stage)


if __name__ == "__main__":
    raise SystemExit(main())
