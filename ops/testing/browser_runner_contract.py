"""Closed suite and filesystem contracts for the candidate browser runner."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject


KNOWN_SUITES: Final = frozenset(
    {"availability", "patient", "runtime-https", "scheduling"}
)
TMPFS_TARGET: Final = PurePosixPath("/", "tmp", "clinic-browser").as_posix()


def filesystem_contract() -> JsonObject:
    """Return the sole writable-path contract accepted for browser runners."""
    return {
        "ipc_mode": "none",
        "root_read_only": True,
        "shm_size_bytes": 0,
        "tmpfs_mounts": [
            {
                "gid": 10001,
                "mode": 0o700,
                "nodev": True,
                "noexec": True,
                "nosuid": True,
                "size_bytes": 268_435_456,
                "target": TMPFS_TARGET,
                "uid": 10001,
            }
        ],
        "writable_paths": [TMPFS_TARGET],
    }


def selected_suites(available: list[str], required: list[str]) -> tuple[str, ...]:
    """Validate sorted cumulative availability and reject absent selections."""
    if (
        available != sorted(set(available))
        or required != sorted(set(required))
        or not set(available) <= KNOWN_SUITES
        or not set(required) <= set(available)
    ):
        message = "browser suite contract failed"
        raise _BrowserSuiteError(message)
    return tuple(required)


class _BrowserSuiteError(RuntimeError):
    pass
