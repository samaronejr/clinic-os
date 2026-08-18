"""Validate the closed static Linux/amd64 ELF launcher contract."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from pathlib import Path

ELF_HEADER_SIZE = 64
ELF_MACHINE_AMD64 = 62
PROGRAM_HEADER_SIZE = 56
PT_DYNAMIC = 2
PT_INTERP = 3
DT_NEEDED = 1


def require_static_amd64_elf(path: Path) -> None:
    """Reject non-ELF, non-amd64, interpreted, or DT_NEEDED launchers."""
    raw = path.read_bytes()
    if (
        len(raw) < ELF_HEADER_SIZE
        or raw[:6] != b"\x7fELF\x02\x01"
        or struct.unpack_from("<H", raw, 18)[0] != ELF_MACHINE_AMD64
    ):
        _fail("Codex launcher is not a Linux/amd64 ELF64 binary")
    offset = struct.unpack_from("<Q", raw, 32)[0]
    entry_size, count = struct.unpack_from("<HH", raw, 54)
    if entry_size != PROGRAM_HEADER_SIZE and count:
        _fail("Codex ELF program-header size is invalid")
    dynamic: tuple[int, int] | None = None
    for index in range(count):
        start = offset + index * entry_size
        if start + entry_size > len(raw):
            _fail("Codex ELF program headers exceed the file")
        kind = struct.unpack_from("<I", raw, start)[0]
        if kind == PT_INTERP:
            _fail("Codex ELF unexpectedly requires PT_INTERP")
        if kind == PT_DYNAMIC:
            dynamic = (
                struct.unpack_from("<QQ", raw, start + 8)[0],
                struct.unpack_from("<Q", raw, start + 32)[0],
            )
    if dynamic is not None:
        _require_no_needed(raw, *dynamic)


def _require_no_needed(raw: bytes, offset: int, size: int) -> None:
    if offset + size > len(raw) or size % 16:
        _fail("Codex ELF dynamic table is malformed")
    for cursor in range(offset, offset + size, 16):
        tag = struct.unpack_from("<Q", raw, cursor)[0]
        if tag == DT_NEEDED:
            _fail("Codex ELF unexpectedly contains DT_NEEDED")
        if tag == 0:
            return


def _fail(message: str) -> Never:
    raise IsolationError(message)
