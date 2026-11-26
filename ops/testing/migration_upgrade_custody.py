"""Create the rehearsal root on a new, unpublished per-invocation tmpfs."""

import os
import re
import stat
import sys

PRIVATE_MODE = 0o700


def create(name: str) -> None:
    """Offer the creator's open descriptor until the shell acknowledges it."""
    if re.fullmatch(r"migration-upgrade-secrets\.[0-9a-f]{32}", name) is None:
        message = "private_run_invalid_name"
        raise ValueError(message)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # The launcher mounts a fresh tmpfs shared by the wrapper and this helper.
    # Its parent directory has no name in the caller's runner directory.
    parent = os.open("/dev/shm", flags)  # noqa: S108 - private namespace mount
    try:
        host = os.environ.get("UPGRADE_HOST_SHM_DEVICE", "")
        if not host.isdecimal() or os.fstat(parent).st_dev == int(host):
            message = "private_creation_namespace_required"
            raise RuntimeError(message)
        with os.scandir(parent) as entries:
            if next(entries, None) is not None:
                message = "private_creation_namespace_not_empty"
                raise RuntimeError(message)
        os.mkdir(name, PRIVATE_MODE, dir_fd=parent)
        root = os.open(name, flags, dir_fd=parent)
        try:
            identity = os.fstat(root)
            if stat.S_IMODE(identity.st_mode) != PRIVATE_MODE:
                message = "private_run_mode_mismatch"
                raise RuntimeError(message)
            sys.stdout.write(
                f"{os.getpid()} {root} {identity.st_dev}:{identity.st_ino}\n"
            )
            sys.stdout.flush()
            sys.stdin.buffer.read(1)
        finally:
            os.close(root)
    finally:
        os.close(parent)


def _require_child_capability(root: int, expected: str, name: str) -> None:
    if name in {"", ".", ".."} or "/" in name:
        message = "private_run_invalid_child"
        raise ValueError(message)
    identity = os.fstat(root)
    if not stat.S_ISDIR(identity.st_mode) or (
        f"{identity.st_dev}:{identity.st_ino}" != expected
    ):
        message = "private_run_identity_mismatch"
        raise RuntimeError(message)


def mkdir_owned(root: int, expected: str, name: str) -> None:
    """Create a direct child of the verified directory, refusing existing names."""
    _require_child_capability(root, expected, name)
    os.mkdir(name, PRIVATE_MODE, dir_fd=root)


def unlink_owned(root: int, expected: str, name: str, *, missing_ok: bool) -> None:
    """Remove a direct child only through the verified creation capability."""
    _require_child_capability(root, expected, name)
    try:
        os.unlink(name, dir_fd=root)
    except FileNotFoundError:
        if not missing_ok:
            raise


if __name__ == "__main__":
    if sys.argv[1] == "unlink":
        unlink_owned(
            int(sys.argv[2]), sys.argv[3], sys.argv[4], missing_ok=sys.argv[5] == "1"
        )
    elif sys.argv[1] == "mkdir":
        for child in sys.argv[4:]:
            mkdir_owned(int(sys.argv[2]), sys.argv[3], child)
    else:
        create(sys.argv[1])
