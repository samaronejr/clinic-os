"""Linux/glibc pidfd_spawn: publish CLONE_PIDFD into caller-owned storage in C."""

from __future__ import annotations

import ctypes as c
import fcntl
import os
from contextlib import ExitStack
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# Public GNU/Linux <spawn.h> ABI. The reserved words are part of the ABI;
# glibc initializes and destroys both structures, never application code.
class _Actions(c.Structure):
    _fields_ = (
        ("allocated", c.c_int),
        ("used", c.c_int),
        ("actions", c.c_void_p),
        ("reserved", c.c_int * 16),
    )


class _Attributes(c.Structure):
    _fields_ = (
        ("flags", c.c_short),
        ("pgrp", c.c_int),
        ("defaults", c.c_ulong * (128 // c.sizeof(c.c_ulong))),
        ("mask", c.c_ulong * (128 // c.sizeof(c.c_ulong))),
        ("priority", c.c_int),
        ("policy", c.c_int),
        ("reserved", c.c_int * 16),
    )


@cache
def _libc() -> c.CDLL:
    lib = c.CDLL("libc.so.6")
    # Deliberately no posix_spawn/fork + pidfd_open fallback.
    lib.pidfd_spawn.argtypes = (
        c.POINTER(c.c_int),
        c.c_char_p,
        c.POINTER(_Actions),
        c.POINTER(_Attributes),
        c.POINTER(c.c_char_p),
        c.POINTER(c.c_char_p),
    )
    lib.pidfd_spawn.restype = c.c_int
    lib.posix_spawn_file_actions_init.argtypes = (c.POINTER(_Actions),)
    lib.posix_spawn_file_actions_destroy.argtypes = (c.POINTER(_Actions),)
    lib.posix_spawn_file_actions_adddup2.argtypes = (
        c.POINTER(_Actions),
        c.c_int,
        c.c_int,
    )
    lib.posix_spawn_file_actions_addclosefrom_np.argtypes = (
        c.POINTER(_Actions),
        c.c_int,
    )
    lib.posix_spawn_file_actions_addchdir_np.argtypes = (
        c.POINTER(_Actions),
        c.c_char_p,
    )
    lib.posix_spawnattr_init.argtypes = (c.POINTER(_Attributes),)
    lib.posix_spawnattr_destroy.argtypes = (c.POINTER(_Attributes),)
    lib.posix_spawnattr_setflags.argtypes = (c.POINTER(_Attributes), c.c_short)
    return lib


def _check(code: int) -> None:
    if code:
        raise OSError(code, os.strerror(code))


def _vector(items: Sequence[str]) -> c.Array[c.c_char_p]:
    encoded = tuple(os.fsencode(s) for s in items)
    if any(b"\0" in value for value in encoded):
        message = "NUL in process input"
        raise ValueError(message)
    return (c.c_char_p * (len(encoded) + 1))(*encoded, None)


@dataclass(frozen=True, slots=True)
class Launch:
    """Explicit argv and descriptor mappings; no shell or implicit inherited FDs."""

    argv: Sequence[str]
    fds: tuple[tuple[int, int], ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    session: bool = True


class PidfdSlot:
    """Own a single kernel output cell without exposing its native ABI."""

    def __init__(self) -> None:
        """Allocate the empty output cell before any child can be created."""
        self._native = c.c_int(-1)

    @property
    def value(self) -> int:
        """Read the published descriptor; -1 means no outstanding handle."""
        return self._native.value

    @value.setter
    def value(self, descriptor: int) -> None:
        self._native.value = descriptor

    def spawn(self, plan: Launch) -> None:
        """Publish into this preallocated cell, not a temporary return value."""
        _acquire_native(self._native, plan)


def acquire(slot: PidfdSlot, plan: Launch) -> None:
    """Create a child into an owner slot already protected by a finally block."""
    slot.spawn(plan)


def _acquire_native(slot: c.c_int, plan: Launch) -> None:
    if slot.value != -1 or not plan.argv or not Path(plan.argv[0]).is_absolute():
        message = "require an empty pidfd slot and an absolute executable"
        raise ValueError(message)
    env = os.environ if plan.env is None else plan.env
    if any(not k or "=" in k for k in env):
        message = "invalid process environment key"
        raise ValueError(message)
    argv = _vector(plan.argv)
    envp = _vector(tuple(f"{k}={v}" for k, v in env.items()))
    actions, attrs = _Actions(), _Attributes()
    lib = _libc()
    with ExitStack() as stack:
        _check(lib.posix_spawn_file_actions_init(c.byref(actions)))
        stack.callback(lib.posix_spawn_file_actions_destroy, c.byref(actions))
        _check(lib.posix_spawnattr_init(c.byref(attrs)))
        stack.callback(lib.posix_spawnattr_destroy, c.byref(attrs))
        if plan.session:
            _check(
                lib.posix_spawnattr_setflags(c.byref(attrs), 0x80)
            )  # POSIX_SPAWN_SETSID
        if plan.cwd is not None:
            _check(
                lib.posix_spawn_file_actions_addchdir_np(
                    c.byref(actions), os.fsencode(plan.cwd)
                )
            )
        # Callers pass stdin/out/err and optionally a private barrier at fd 3.
        # Duplicate sources away from the target range to avoid action collisions.
        targets = [target for _, target in plan.fds]
        if targets not in ([0, 1, 2], [0, 1, 2, 3]):
            message = "incomplete or unexpected spawn descriptor map"
            raise ValueError(message)
        for source, target in plan.fds:
            duplicate = fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, 4)
            stack.callback(os.close, duplicate)
            _check(
                lib.posix_spawn_file_actions_adddup2(
                    c.byref(actions), duplicate, target
                )
            )
        _check(
            lib.posix_spawn_file_actions_addclosefrom_np(c.byref(actions), len(targets))
        )
        # glibc creates the child with CLONE_PIDFD. Its successful return writes
        # the fd into the existing slot before Python can lose the return value.
        _check(
            lib.pidfd_spawn(
                c.byref(slot), argv[0], c.byref(actions), c.byref(attrs), argv, envp
            )
        )
