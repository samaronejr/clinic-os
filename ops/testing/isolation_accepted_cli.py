"""Dispatch terminal reconciliation and accepted-success close grammar."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_cli_authority import (
    canonical_final_receipt,
    canonical_terminal_final,
)
from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_terminal_reconcile import reconcile_terminal_final

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_common import JsonObject

TERMINAL_ARGUMENT_COUNT: Final = 5
SAME_BOOT_CLOSE_ARGUMENT_COUNT: Final = 3
CHANGED_BOOT_CLOSE_ARGUMENT_COUNT: Final = 5


def dispatch_accepted_command(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> bool:
    """Dispatch only the frozen terminal and accepted-close forms."""
    if arguments[:2] == ("reconcile", "--terminal-final"):
        _dispatch_terminal(arguments, ledger_path, inventory_reader)
        return True
    if arguments and arguments[0] == "close" and "--final-receipt" in arguments:
        _dispatch_close(arguments, ledger_path, inventory_reader)
        return True
    return False


def _dispatch_terminal(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> None:
    if len(arguments) != TERMINAL_ARGUMENT_COUNT or arguments[3] != "--sha":
        _fail("invalid isolation-ledger command grammar")
    path = reconcile_terminal_final(
        ledger_path,
        terminal_final=canonical_terminal_final(ledger_path, arguments[2]),
        sha=arguments[4],
        inventory_reader=inventory_reader,
    )
    sys.stdout.write(f"{path}\n")


def _dispatch_close(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> None:
    terminal: Path | None = None
    receipt_index = 1
    if len(arguments) == CHANGED_BOOT_CLOSE_ARGUMENT_COUNT:
        if arguments[1] != "--terminal-revalidation":
            _fail("invalid isolation-ledger command grammar")
        terminal = Path(arguments[2])
        receipt_index = 3
    elif len(arguments) != SAME_BOOT_CLOSE_ARGUMENT_COUNT:
        _fail("invalid isolation-ledger command grammar")
    if arguments[receipt_index] != "--final-receipt":
        _fail("invalid isolation-ledger command grammar")
    close_accepted_attempt(
        ledger_path,
        final_receipt=canonical_final_receipt(
            ledger_path,
            arguments[receipt_index + 1],
        ),
        terminal_revalidation=terminal,
        inventory_reader=inventory_reader,
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
