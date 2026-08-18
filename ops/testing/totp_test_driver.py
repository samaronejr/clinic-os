"""Test-only private-FD bridge that emits one synthetic TOTP code.

This module is never an HTTP, management, or product API. Each process reads a
single synthetic context frame from a private input descriptor, resolves exactly
one FORCE-RLS visible device under the runtime application role, writes the code
bytes to a private output descriptor, and closes both descriptors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import sys
import time
from typing import Final, Never

import psycopg

CODE_LENGTH: Final = 6
ARGUMENT_COUNT: Final = 6
MODES: Final = frozenset({"pending-enrollment", "confirmed-next-counter"})
CONTEXT_KEYS: Final = frozenset({"dsn", "last_t", "mode", "tenant_id", "user_id"})
MAX_CONTEXT_BYTES: Final = 4096
MIN_DESCRIPTOR: Final = 3
DEVICE_QUERY: Final = (
    "SELECT id, key, step, t0, digits, drift, last_t "
    "FROM clinic_app.otp_totp_totpdevice "
    "WHERE user_id = %s::uuid AND confirmed = %s ORDER BY id"
)


class TotpDriverError(RuntimeError):
    """Reject every malformed invocation without echoing synthetic secrets."""

    def __init__(self) -> None:
        """Expose one stable non-identifying driver failure message."""
        super().__init__("totp test driver invocation rejected")


def _fail() -> Never:
    raise TotpDriverError


def parse_arguments(arguments: list[str]) -> tuple[str, int, int]:
    """Return the exact mode and the two private descriptor numbers."""
    if len(arguments) != ARGUMENT_COUNT:
        _fail()
    if arguments[0] != "--mode" or arguments[2] != "--context-fd":
        _fail()
    if arguments[4] != "--code-fd":
        _fail()
    mode = arguments[1]
    if mode not in MODES:
        _fail()
    return mode, _descriptor(arguments[3]), _descriptor(arguments[5])


def _descriptor(value: str) -> int:
    if not value.isdigit():
        _fail()
    number = int(value)
    if number < MIN_DESCRIPTOR:
        _fail()
    return number


def read_context(context_fd: int, mode: str) -> dict[str, object]:
    """Read and validate the single bounded synthetic context frame."""
    with os.fdopen(context_fd, "rb", closefd=True) as stream:
        raw = stream.read(MAX_CONTEXT_BYTES + 1)
    if not raw or len(raw) > MAX_CONTEXT_BYTES:
        _fail()
    document: object = json.loads(raw)
    if not isinstance(document, dict) or set(document) != set(CONTEXT_KEYS):
        _fail()
    if document.get("mode") != mode:
        _fail()
    for key in ("dsn", "tenant_id", "user_id"):
        if not isinstance(document.get(key), str) or not document[key]:
            _fail()
    last_t = document.get("last_t")
    if isinstance(last_t, bool) or not isinstance(last_t, int) or last_t < -1:
        _fail()
    return document


def _totp_code(key: bytes, counter: int, digits: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{truncated % (10**digits):0{digits}d}"


def _selected_device(rows: list[tuple[object, ...]]) -> tuple[object, ...]:
    if len(rows) != 1:
        _fail()
    return rows[0]


def resolve_code(context: dict[str, object], mode: str) -> str:
    """Open one read-only app-role transaction and derive the single code."""
    confirmed = mode == "confirmed-next-counter"
    with psycopg.connect(str(context["dsn"])) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
                "pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(context["tenant_id"]), str(context["user_id"])],
            )
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(DEVICE_QUERY, [str(context["user_id"]), confirmed])
            device = _selected_device(cursor.fetchall())
        connection.rollback()
    return _code_for_device(device, context, confirmed=confirmed)


def _code_for_device(
    device: tuple[object, ...],
    context: dict[str, object],
    *,
    confirmed: bool,
) -> str:
    _, key, step, t0, digits, drift, device_last_t = device
    if not isinstance(key, str) or not isinstance(step, int):
        _fail()
    if not isinstance(t0, int) or not isinstance(digits, int):
        _fail()
    if not isinstance(drift, int) or not isinstance(device_last_t, int):
        _fail()
    if digits != CODE_LENGTH:
        _fail()
    counter = int((time.time() - t0) // step) + drift
    if confirmed:
        requested = context["last_t"]
        if not isinstance(requested, int) or counter <= requested:
            _fail()
        if counter <= device_last_t:
            _fail()
    return _totp_code(bytes.fromhex(key), counter, digits)


def emit_code(code_fd: int, code: str) -> None:
    """Write exactly the code bytes to the private descriptor and close it."""
    payload = code.encode("ascii")
    if len(payload) != CODE_LENGTH:
        _fail()
    with os.fdopen(code_fd, "wb", closefd=True) as stream:
        stream.write(payload)


def run(arguments: list[str]) -> None:
    """Execute one complete private-FD code delivery."""
    mode, context_fd, code_fd = parse_arguments(arguments)
    context = read_context(context_fd, mode)
    emit_code(code_fd, resolve_code(context, mode))


def main() -> int:
    """Return two on any rejection without writing diagnostic bytes."""
    try:
        run(sys.argv[1:])
    except (TotpDriverError, OSError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
