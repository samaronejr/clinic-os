"""Own the closed, stable-lock-serialized Phase 1A isolation ledger."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Sequence

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.testing.isolation_accepted_cli import (  # noqa: E402
    dispatch_accepted_command,
)
from ops.testing.isolation_archive_cli import dispatch_archive_command  # noqa: E402
from ops.testing.isolation_candidate_publication import (  # noqa: E402
    publish_candidate_envelope,
)
from ops.testing.isolation_claim_transitions import (  # noqa: E402
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import IsolationError  # noqa: E402
from ops.testing.isolation_host_inventory import capture_host_inventory  # noqa: E402
from ops.testing.isolation_refresh import verify_claim  # noqa: E402
from ops.testing.isolation_rejection_cli import (  # noqa: E402
    dispatch_rejection_command,
)
from ops.testing.isolation_runner_lifecycle import (  # noqa: E402
    create_runner,
    discard_runner,
)
from ops.testing.isolation_same_boot_coordinator import (  # noqa: E402
    reconcile_same_boot,
)
from ops.testing.isolation_snapshot import (  # noqa: E402, F401
    LEDGER_NAME,
    SnapshotRequest,
    snapshot_ledger,
)
from ops.testing.isolation_snapshot_cli import (  # noqa: E402
    dispatch_snapshot_command,
)
from ops.testing.isolation_stale_recovery import reconcile_stale_boot  # noqa: E402
from ops.testing.isolation_tracked_snapshot import (  # noqa: E402, F401
    TrackedSnapshotRequest,
    snapshot_tracked_ledger,
)

SINGLE_OPTION_ARGUMENT_COUNT: Final = 3
DOUBLE_OPTION_ARGUMENT_COUNT: Final = 5
VERIFY_ARGUMENT_COUNT: Final = 4


def run_cli(arguments: Sequence[str]) -> int:
    """Dispatch one exact isolation-ledger command without grammar aliases."""
    try:
        _dispatch(tuple(arguments))
    except (IsolationError, OSError, ValueError) as error:
        message = f"isolation-ledger: {error}\n"
        sys.stderr.write(message)
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


def _main() -> int:
    return run_cli(sys.argv[1:])


def _dispatch(arguments: tuple[str, ...]) -> None:
    if dispatch_snapshot_command(arguments):
        return
    _dispatch_ordinary(arguments)


def _dispatch_ordinary(arguments: tuple[str, ...]) -> None:
    archive_command = bool(arguments and arguments[0] == "archive-rollover")
    ledger_path = _ordinary_ledger_path(allow_absent=archive_command)
    if dispatch_archive_command(arguments, ledger_path, capture_host_inventory):
        return
    if dispatch_accepted_command(arguments, ledger_path, capture_host_inventory):
        return
    if dispatch_rejection_command(arguments, ledger_path, capture_host_inventory):
        return
    if arguments and arguments[0].startswith("runner-"):
        _dispatch_runner(arguments, ledger_path)
        return
    if _dispatch_verify_or_reconcile(arguments, ledger_path):
        return
    if _dispatch_claim_command(arguments, ledger_path):
        return
    _fail("invalid isolation-ledger command grammar")


def _dispatch_claim_command(
    arguments: tuple[str, ...],
    ledger_path: Path,
) -> bool:
    if len(arguments) == SINGLE_OPTION_ARGUMENT_COUNT and arguments[:2] == (
        "claim",
        "--spec",
    ):
        claim_id = reserve_claim(ledger_path, Path(arguments[2]))
        sys.stdout.write(f"{claim_id}\n")
        return True
    if len(arguments) == SINGLE_OPTION_ARGUMENT_COUNT and arguments[:2] == (
        "release",
        "--claim",
    ):
        release_claim(ledger_path, arguments[2])
        return True
    if len(arguments) == DOUBLE_OPTION_ARGUMENT_COUNT and arguments[:2] == (
        "activate",
        "--claim",
    ):
        if arguments[3] != "--observed":
            _fail("invalid isolation-ledger command grammar")
        activate_claim(ledger_path, arguments[2], Path(arguments[4]))
        return True
    if len(arguments) == DOUBLE_OPTION_ARGUMENT_COUNT and arguments[:2] == (
        "candidate-publish",
        "--claim",
    ):
        if arguments[3] != "--staged-envelope":
            _fail("invalid isolation-ledger command grammar")
        raw = publish_candidate_envelope(
            ledger_path,
            arguments[2],
            Path(arguments[4]),
        )
        sys.stdout.buffer.write(raw)
        return True
    return False


def _dispatch_verify_or_reconcile(
    arguments: tuple[str, ...],
    ledger_path: Path,
) -> bool:
    if len(arguments) == VERIFY_ARGUMENT_COUNT and arguments[:3] == (
        "verify",
        "--refresh",
        "--claim",
    ):
        verify_claim(ledger_path, arguments[3], refresh=True)
        return True
    if arguments == ("reconcile",):
        reconcile_same_boot(ledger_path)
        return True
    if arguments == ("reconcile", "--stale-boot"):
        reconcile_stale_boot(ledger_path)
        return True
    return False


def _dispatch_runner(arguments: tuple[str, ...], ledger_path: Path) -> None:
    if len(arguments) == SINGLE_OPTION_ARGUMENT_COUNT and arguments[:2] == (
        "runner-create",
        "--claim",
    ):
        create_runner(ledger_path, arguments[2])
        return
    if (
        len(arguments) == DOUBLE_OPTION_ARGUMENT_COUNT
        and arguments[:2] == ("runner-discard", "--claim")
        and arguments[3] == "--reason"
    ):
        discard_runner(ledger_path, arguments[2], arguments[4])
        return
    _fail("invalid isolation-ledger command grammar")


def _ordinary_ledger_path(*, allow_absent: bool = False) -> Path:
    worktree = Path.cwd().resolve(strict=True)
    authority_link = worktree / ".omo"
    if not authority_link.is_symlink():
        _fail("current worktree lacks its authenticated .omo binding")
    authority_root = authority_link.resolve(strict=True)
    ledger_path = authority_root / "evidence" / LEDGER_NAME
    if allow_absent:
        if ledger_path.is_symlink():
            _fail("canonical isolation ledger path is a symlink")
        if ledger_path.exists() and ledger_path.resolve(strict=True) != ledger_path:
            _fail("canonical isolation ledger path is noncanonical")
        return ledger_path
    return ledger_path.resolve(strict=True)


if __name__ == "__main__":
    raise SystemExit(_main())
