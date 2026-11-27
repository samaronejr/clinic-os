"""Confine one rehearsal child with a Landlock ruleset the kernel enforces.

Mount flags, bind mounts and name checks all belong to a namespace that the
invoking user owns, so a process of the same user can reach into that namespace
and loosen them. A Landlock domain is different: it is attached to the child's
own credentials, it cannot be loosened by anyone, and every open, create,
remove, rename and truncate is checked against it at access time. A late mount,
a planted link or a descriptor reopened through ``/proc`` all resolve to the
same object, and the object is judged by where it sits, not by how it was named.

The policy is one write root, handed over as a descriptor the wrapper already
holds, read and execute access to the listed inputs only, optionally TCP to
named ports only, and no UDP at all (a fetching child may reach port 53 for name
resolution). Everything else is refused. There is no fallback: a kernel without
Landlock, or one too old to cover truncation, cross-directory renames and UDP,
is refused by name so the caller can stop instead of running unconfined.

This file is deliberately self-contained, with standard-library imports only,
because the sandbox launcher embeds its source in the stub that runs inside the
sandbox, so no file on a path the invoking user can rewrite decides the policy.
"""

import ctypes
import errno
import os
import stat
from pathlib import Path

MIN_ABI = 10  # UDP bind and connect/send are covered from ABI 10
DNS_PORT = 53
SYS_CREATE_RULESET = 444
SYS_ADD_RULE = 445
SYS_RESTRICT_SELF = 446
CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH = 1
RULE_NET_PORT = 2
PR_SET_NO_NEW_PRIVS = 38

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13  # ABI 2
TRUNCATE = 1 << 14  # ABI 3
IOCTL_DEV = 1 << 15  # ABI 5
RESOLVE_UNIX = 1 << 16  # ABI 9: connecting to a pathname socket
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4
NET_BIND_UDP = 1 << 2  # ABI 10
NET_CONNECT_SEND_UDP = 1 << 3  # ABI 10
SCOPE_ABSTRACT_UNIX = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1  # ABI 6

MAKE_ANY = (
    MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM
)
READS = EXECUTE | READ_FILE | READ_DIR
WRITES = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_ANY
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
SYSTEM_DEVICES = "/dev"


class LandlockUnavailableError(OSError):
    """The kernel cannot confine this child, so it must not run."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


class _NetPort(ctypes.Structure):
    _fields_ = (("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64))


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi(libc: ctypes.CDLL | None = None) -> int:
    """Return the kernel's Landlock ABI, or refuse by name when it is unusable."""
    found = libc or _libc()
    version = found.syscall(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION)
    if version < 0:
        code = ctypes.get_errno()
        message = f"landlock_unavailable: {errno.errorcode.get(code, code)}"
        raise LandlockUnavailableError(code, message)
    if version < MIN_ABI:
        message = f"landlock_abi_too_old: {version} < {MIN_ABI}"
        raise LandlockUnavailableError(errno.ENOSYS, message)
    return int(version)


def _filesystem_rights(version: int) -> int:
    rights = READS | WRITES
    for needed, bit in ((2, REFER), (3, TRUNCATE), (5, IOCTL_DEV), (9, RESOLVE_UNIX)):
        if version >= needed:
            rights |= bit
    return rights


def _check(result: int, what: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"landlock_{what}_failed: {os.strerror(code)}")
    return result


def _rule_path(libc: ctypes.CDLL, ruleset: int, target: int | str, access: int) -> None:
    """Allow ``access`` beneath one directory, or on one file, named by descriptor."""
    held = (
        os.open(target, os.O_PATH | os.O_CLOEXEC) if isinstance(target, str) else target
    )
    try:
        if not stat.S_ISDIR(os.fstat(held).st_mode):
            access &= FILE_RIGHTS
        attribute = _PathBeneath(access, held)
        _check(
            libc.syscall(
                SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(attribute), 0
            ),
            "add_rule",
        )
    finally:
        if isinstance(target, str):
            os.close(held)


def _rule_net(libc: ctypes.CDLL, ruleset: int, access: int, port: int) -> None:
    allowed = _NetPort(access, port)
    _check(
        libc.syscall(SYS_ADD_RULE, ruleset, RULE_NET_PORT, ctypes.byref(allowed), 0),
        "add_net_rule",
    )


def apply(
    write_roots: list[int],
    read_paths: list[str],
    connect_ports: list[int] | None = None,
    libc: ctypes.CDLL | None = None,
) -> int:
    """Confine this process and everything it starts; return the ABI that applied.

    ``write_roots`` are descriptors of directories that may be written, created
    in, renamed within and removed from. ``read_paths`` may be read and
    executed. ``/dev`` is writable as a device directory only. With
    ``connect_ports`` TCP may connect to those ports and to nothing else; without
    it TCP is unrestricted and UDP may only reach port 53. UDP is otherwise
    refused in both modes.
    """
    found = libc or _libc()
    version = abi(found)
    net = NET_BIND_UDP | NET_CONNECT_SEND_UDP
    if connect_ports is not None:
        net |= NET_BIND_TCP | NET_CONNECT_TCP
    scoped = SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL if version >= 6 else 0  # noqa: PLR2004
    attribute = _RulesetAttr(_filesystem_rights(version), net, scoped)
    ruleset = found.syscall(
        SYS_CREATE_RULESET, ctypes.byref(attribute), ctypes.sizeof(attribute), 0
    )
    if ruleset < 0 and ctypes.get_errno() == errno.EINVAL:
        # An ABI newer than this table names a right this build does not know.
        attribute.handled_access_fs &= ~RESOLVE_UNIX
        ruleset = found.syscall(
            SYS_CREATE_RULESET, ctypes.byref(attribute), ctypes.sizeof(attribute), 0
        )
    ruleset = _check(ruleset, "create_ruleset")
    try:
        everything = _filesystem_rights(version)
        for root in write_roots:
            _rule_path(found, ruleset, root, everything)
        for path in read_paths:
            if Path(path).exists():
                _rule_path(found, ruleset, path, READS)
        if Path(SYSTEM_DEVICES).is_dir():
            devices = WRITE_FILE | TRUNCATE | IOCTL_DEV | READS
            _rule_path(found, ruleset, SYSTEM_DEVICES, devices)
        for port in connect_ports or []:
            _rule_net(found, ruleset, NET_CONNECT_TCP, port)
        if connect_ports is None:
            # Name resolution only. Both UDP rights are handled, so the resolver's
            # automatic ephemeral source port needs BIND_UDP on port 0.
            _rule_net(found, ruleset, NET_CONNECT_SEND_UDP, DNS_PORT)
            _rule_net(found, ruleset, NET_BIND_UDP, 0)
        _check(found.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        _check(found.syscall(SYS_RESTRICT_SELF, ruleset, 0), "restrict_self")
    finally:
        os.close(ruleset)
    return version
