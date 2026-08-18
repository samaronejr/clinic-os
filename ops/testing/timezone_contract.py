"""Validate the immutable timezone wheel recorded by the project lock."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final, Never

TZDATA_PACKAGE: Final = b'name = "tzdata"\nversion = "2026.3"'
ARGUMENT_COUNT: Final = 2
TZDATA_WHEEL: Final = (
    b'{ url = "https://files.pythonhosted.org/packages/e5/6d/'
    b"b53b99a9f2766d095985947a5782f1702cabb129a34f7a802d7197af832f/"
    b'tzdata-2026.3-py2.py3-none-any.whl", '
    b'hash = "sha256:dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e'
    b'54931", '
    b'size = 348168, upload-time = "2026-07-10T08:50:36.46Z" },'
)


def validate_tzdata_lock(lock_bytes: bytes) -> None:
    """Reject any version, URL, digest, size, or timestamp substitution."""
    if lock_bytes.count(TZDATA_PACKAGE) != 1 or lock_bytes.count(TZDATA_WHEEL) != 1:
        _fail()


def _main() -> None:
    if len(sys.argv) != ARGUMENT_COUNT:
        _fail()
    validate_tzdata_lock(Path(sys.argv[1]).read_bytes())


def _fail() -> Never:
    message = "timezone lock contract failed"
    raise _TimezoneLockError(message)


class _TimezoneLockError(RuntimeError):
    pass


if __name__ == "__main__":
    _main()
