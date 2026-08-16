"""Parse and dispatch the closed ordinary and USER rejection command forms."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_cli_authority import canonical_control_root
from ops.testing.isolation_close import close_rejected_attempt
from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_rejection import (
    RejectionRequest,
    build_rejection_context,
    reject_attempt,
)
from ops.testing.isolation_user_fix import (
    UserFixAuthorizationRequest,
    authorize_user_fix,
    build_user_rejection_context,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_common import JsonObject

CONTEXT_ARGUMENT_COUNT: Final = 7
USER_CONTEXT_ARGUMENT_COUNT: Final = 10
AUTHORIZE_ARGUMENT_COUNT: Final = 7
REJECT_MINIMUM_ARGUMENT_COUNT: Final = 11
REJECT_TAIL_ARGUMENT_COUNT: Final = 6
USER_PREFIX_ARGUMENT_COUNT: Final = 3
USER_CLOSE_ARGUMENT_COUNT: Final = 3
USER_REBOOT_CLOSE_ARGUMENT_COUNT: Final = 5


def dispatch_rejection_command(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> bool:
    """Dispatch only fixed rejection-context, reject, and close grammar."""
    if arguments and arguments[0] == "authorize-user-fix":
        _dispatch_authorize(arguments, ledger_path, inventory_reader)
        return True
    if arguments and arguments[0] == "rejection-context":
        _dispatch_context(arguments, ledger_path, inventory_reader)
        return True
    if arguments and arguments[0] == "reject":
        request = _parse_rejection(arguments)
        digest = reject_attempt(
            ledger_path,
            request,
            inventory_reader=inventory_reader,
        )
        sys.stdout.write(f"{digest}\n")
        return True
    if arguments and arguments[0] == "close":
        _dispatch_close(arguments, ledger_path, inventory_reader)
        return True
    return False


def _dispatch_authorize(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> None:
    expected = (
        "authorize-user-fix",
        "--sha",
        "--terminal-revalidation",
        "--control-root",
    )
    if (
        len(arguments) != AUTHORIZE_ARGUMENT_COUNT
        or tuple(arguments[index] for index in (0, 1, 3, 5)) != expected
    ):
        _fail("invalid isolation-ledger command grammar")
    binding = authorize_user_fix(
        ledger_path,
        UserFixAuthorizationRequest(
            sha=arguments[2],
            terminal_revalidation=Path(arguments[4]),
            control_root=canonical_control_root(ledger_path, arguments[6]),
        ),
        inventory_reader=inventory_reader,
    )
    sys.stdout.write(f"{binding}\n")


def _dispatch_context(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> None:
    if len(arguments) == CONTEXT_ARGUMENT_COUNT and arguments[0:2] == (
        "rejection-context",
        "--sha",
    ):
        if arguments[3] != "--control-root" or arguments[5:] != (
            "--format",
            "nul",
        ):
            _fail("invalid isolation-ledger command grammar")
        fields = build_rejection_context(
            ledger_path,
            sha=arguments[2],
            control_root=canonical_control_root(ledger_path, arguments[4]),
            inventory_reader=inventory_reader,
        )
    elif (
        len(arguments) == USER_CONTEXT_ARGUMENT_COUNT
        and arguments[0:3]
        == ("rejection-context", "--user-requested-fix", "--user-authorization")
        and arguments[4] == "--sha"
        and arguments[6] == "--control-root"
        and arguments[8:] == ("--format", "nul")
    ):
        fields = build_user_rejection_context(
            ledger_path,
            sha=arguments[5],
            control_root=canonical_control_root(ledger_path, arguments[7]),
            user_authorization=Path(arguments[3]),
            inventory_reader=inventory_reader,
        )
    else:
        _fail("invalid isolation-ledger command grammar")
    sys.stdout.buffer.write(b"\0".join(item.encode() for item in fields) + b"\0")


def _parse_rejection(arguments: tuple[str, ...]) -> RejectionRequest:
    user_authorization: Path | None = None
    sha_index = 1
    if (
        len(arguments) >= USER_PREFIX_ARGUMENT_COUNT
        and arguments[1] == "--user-authorization"
    ):
        user_authorization = Path(arguments[2])
        sha_index = 3
    if (
        len(arguments) < REJECT_MINIMUM_ARGUMENT_COUNT + sha_index - 1
        or arguments[sha_index] != "--sha"
    ):
        _fail("invalid isolation-ledger command grammar")
    index = sha_index + 2
    reasons: list[str] = []
    while index + 1 < len(arguments) and arguments[index] == "--reason":
        reasons.append(arguments[index + 1])
        index += 2
    expected_tail = (
        "--inputs-sha256",
        "--pre-f4-sha256",
        "--final-sha256",
    )
    if (
        len(arguments) - index != REJECT_TAIL_ARGUMENT_COUNT
        or tuple(arguments[index::2]) != expected_tail
    ):
        _fail("invalid isolation-ledger command grammar")
    return RejectionRequest(
        sha=arguments[sha_index + 1],
        reasons=tuple(reasons),
        inputs_sha256=_parse_optional_sha(arguments[index + 1]),
        pre_f4_sha256=_parse_optional_sha(arguments[index + 3]),
        final_sha256=_parse_optional_sha(arguments[index + 5]),
        user_authorization=user_authorization,
    )


def _dispatch_close(
    arguments: tuple[str, ...],
    ledger_path: Path,
    inventory_reader: Callable[[], JsonObject],
) -> None:
    if arguments == ("close",):
        close_rejected_attempt(ledger_path, inventory_reader=inventory_reader)
        return
    if (
        len(arguments)
        not in (
            USER_CLOSE_ARGUMENT_COUNT,
            USER_REBOOT_CLOSE_ARGUMENT_COUNT,
        )
        or arguments[1] != "--user-authorization"
    ):
        _fail("invalid isolation-ledger command grammar")
    terminal: Path | None = None
    if len(arguments) == USER_REBOOT_CLOSE_ARGUMENT_COUNT:
        if arguments[3] != "--terminal-revalidation":
            _fail("invalid isolation-ledger command grammar")
        terminal = Path(arguments[4])
    close_rejected_attempt(
        ledger_path,
        inventory_reader=inventory_reader,
        user_authorization=Path(arguments[2]),
        terminal_revalidation=terminal,
    )


def _parse_optional_sha(value: str) -> str | None:
    return None if value == "none" else value


def _fail(message: str) -> Never:
    raise IsolationError(message)
