"""Parse and dispatch the exact rejected-attempt archive command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_archive import ArchiveRequest, archive_rollover
from ops.testing.isolation_cli_authority import canonical_control_root
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

ARCHIVE_ARGUMENT_COUNT: Final = 9


def dispatch_archive_command(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> bool:
    """Accept only the fixed closed-attempt/SHA/control/spec selector order."""
    if not arguments or arguments[0] != "archive-rollover":
        return False
    expected = (
        "archive-rollover",
        "--closed-attempt",
        "--rejected-sha",
        "--control-root",
        "--rejection-spec-sha256",
    )
    received = tuple(arguments[index] for index in (0, 1, 3, 5, 7))
    if len(arguments) != ARCHIVE_ARGUMENT_COUNT or received != expected:
        _fail("invalid isolation-ledger command grammar")
    archive_rollover(
        ledger_path,
        ArchiveRequest(
            closed_attempt=arguments[2],
            rejected_sha=arguments[4],
            control_root=canonical_control_root(ledger_path, arguments[6]),
            rejection_spec_sha256=arguments[8],
        ),
        inventory_reader=inventory_reader,
    )
    return True


def _fail(message: str) -> Never:
    raise IsolationError(message)
