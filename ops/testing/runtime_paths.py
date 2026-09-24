"""Private temporary directories for active verification runs."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_common import MODE_DIRECTORY, IsolationError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_PURPOSE: Final = re.compile(r"[a-z][a-z0-9-]{0,63}")


@contextmanager
def runtime_directory(run_root: Path, *, purpose: str) -> Generator[Path]:
    """Allocate one disposable child of an existing private, canonical run root.

    The caller supplies the task-owned root and retains exclusive control of it
    throughout this context. Yielded Paths are ordinary filesystem paths, not
    capabilities against later same-user renames. Allocation uses a held root
    descriptor; cleanup refuses root/child substitution and never follows links.
    Existing siblings and the run root itself are never removed or repaired.
    """
    if _PURPOSE.fullmatch(purpose) is None:
        message = "runtime directory purpose must be one safe component"
        raise IsolationError(message)
    with _open_root(run_root) as root_descriptor:
        name = f"{purpose}-{secrets.token_hex(16)}"
        _require_root(run_root, root_descriptor)
        try:
            os.mkdir(name, MODE_DIRECTORY, dir_fd=root_descriptor)
        except OSError as error:
            message = "runtime child cannot be exclusively allocated"
            raise IsolationError(message) from error
        with _open_child(root_descriptor, name) as child_descriptor:
            _require_root(run_root, root_descriptor)
            _require_child(root_descriptor, name, child_descriptor)
            try:
                yield run_root / name
            finally:
                _require_root(run_root, root_descriptor)
                _require_child(root_descriptor, name, child_descriptor)
                _clear_contents(child_descriptor)
                _require_child(root_descriptor, name, child_descriptor)
                os.rmdir(name, dir_fd=root_descriptor)


@contextmanager
def _open_root(root: Path) -> Generator[int]:
    if not root.is_absolute() or ".." in root.parts or root.anchor != "/":
        message = "runtime root must be an absolute canonical path"
        raise IsolationError(message)
    with ExitStack() as descriptors:
        try:
            descriptor = os.open("/", _DIRECTORY_FLAGS)
            _ = descriptors.callback(os.close, descriptor)
            for part in root.parts[1:]:
                descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                _ = descriptors.callback(os.close, descriptor)
            _require_private(descriptor)
        except OSError as error:
            message = "runtime root must exist without symlink traversal"
            raise IsolationError(message) from error
        yield descriptor


@contextmanager
def _open_child(parent_descriptor: int, name: str) -> Generator[int]:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        message = "runtime child cannot be opened without following links"
        raise IsolationError(message) from error
    try:
        _require_private(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def _require_private(descriptor: int) -> None:
    identity = os.fstat(descriptor)
    if (
        identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
        or stat.S_IMODE(identity.st_mode) != MODE_DIRECTORY
    ):
        message = "runtime directory must be executor-owned with mode 0700"
        raise IsolationError(message)


def _require_root(root: Path, descriptor: int) -> None:
    with _open_root(root) as current:
        if not os.path.samestat(os.fstat(current), os.fstat(descriptor)):
            message = "runtime root identity changed; preserving resources"
            raise IsolationError(message)


def _require_child(parent: int, name: str, descriptor: int) -> None:
    _require_private(descriptor)
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        message = "runtime child identity unavailable; preserving resources"
        raise IsolationError(message) from error
    if not os.path.samestat(named, os.fstat(descriptor)):
        message = "runtime child identity changed; preserving resources"
        raise IsolationError(message)


def _clear_contents(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        children = [
            (entry.name, entry.stat(follow_symlinks=False)) for entry in entries
        ]
    for name, identity in children:
        if identity.st_uid != os.geteuid() or identity.st_gid != os.getegid():
            message = "runtime child contains foreign-owned entries; preserving them"
            raise IsolationError(message)
        if stat.S_ISDIR(identity.st_mode):
            shutil.rmtree(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
