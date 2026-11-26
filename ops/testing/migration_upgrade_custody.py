"""Create and use the rehearsal's private run directory only through its descriptor."""

import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO

PRIVATE_MODE = 0o700
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def create(name: str) -> None:
    """Offer the creator's open descriptor until the shell acknowledges it."""
    if re.fullmatch(r"migration-upgrade-secrets\.[0-9a-f]{32}", name) is None:
        message = "private_run_invalid_name"
        raise ValueError(message)
    # The launcher mounts a fresh tmpfs shared by the wrapper and this helper.
    # Its parent directory has no name in the caller's runner directory.
    parent = os.open("/dev/shm", DIRECTORY_FLAGS)  # noqa: S108 - private mount
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
        root = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
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


def _identity(descriptor: int) -> str:
    identity = os.fstat(descriptor)
    return f"{identity.st_dev}:{identity.st_ino}"


def _require_directory(descriptor: int, expected: str) -> None:
    identity = os.fstat(descriptor)
    if not stat.S_ISDIR(identity.st_mode) or _identity(descriptor) != expected:
        message = "private_run_identity_mismatch"
        raise RuntimeError(message)


def _require_child_name(name: str) -> None:
    if name in {"", ".", ".."} or "/" in name:
        message = "private_run_invalid_child"
        raise ValueError(message)


def mkdir_owned(root: int, expected: str, name: str) -> str:
    """Create a child of the verified directory and return its held identity."""
    _require_directory(root, expected)
    _require_child_name(name)
    os.mkdir(name, PRIVATE_MODE, dir_fd=root)
    child = os.open(name, DIRECTORY_FLAGS, dir_fd=root)
    try:
        return _identity(child)
    finally:
        os.close(child)


def unlink_owned(root: int, expected: str, name: str, *, missing_ok: bool) -> None:
    """Remove a direct child only through the verified creation capability."""
    _require_directory(root, expected)
    _require_child_name(name)
    try:
        os.unlink(name, dir_fd=root)
    except FileNotFoundError:
        if not missing_ok:
            raise


def symlink_owned(directory: str, expected: str, name: str, target: str) -> None:
    """Create one link in a held directory; an existing entry is refused."""
    _require_child_name(name)
    held = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _require_directory(held, expected)
        os.symlink(target, name, dir_fd=held)
    finally:
        os.close(held)


def require_empty(directory: str, expected: str) -> None:
    """Refuse a held directory that already contains any entry."""
    held = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _require_directory(held, expected)
        with os.scandir(held) as entries:
            if next(entries, None) is not None:
                message = "private_tree_foreign_entry"
                raise RuntimeError(message)
    finally:
        os.close(held)


def check_tree(root: int, expected: str, children: dict[str, str]) -> None:
    """Refuse a run directory that gained a link or lost a created child."""
    _require_directory(root, expected)
    seen = set()
    with os.scandir(root) as entries:
        for entry in entries:
            identity = os.lstat(entry.name, dir_fd=root)
            if stat.S_ISLNK(identity.st_mode):
                message = f"private_tree_foreign_entry: {entry.name}"
                raise RuntimeError(message)
            if entry.name in children:
                seen.add(entry.name)
                if (
                    not stat.S_ISDIR(identity.st_mode)
                    or f"{identity.st_dev}:{identity.st_ino}" != children[entry.name]
                ):
                    message = f"private_tree_replaced: {entry.name}"
                    raise RuntimeError(message)
    if seen != set(children):
        message = "private_tree_missing_entry"
        raise RuntimeError(message)


class _Extractor:
    """Unpack a tar stream into a directory this run created, never by name."""

    def __init__(self, top: int) -> None:
        self.top = top
        self.created = {self._key(os.fstat(top))}

    @staticmethod
    def _key(identity: os.stat_result) -> tuple[int, int]:
        return identity.st_dev, identity.st_ino

    def _descend(self, parts: tuple[str, ...]) -> int:
        """Open (creating as needed) each directory without following links."""
        current = os.dup(self.top)
        try:
            for part in parts:
                try:
                    os.mkdir(part, PRIVATE_MODE, dir_fd=current)
                except FileExistsError:
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
                    if self._key(os.fstat(child)) not in self.created:
                        os.close(child)
                        message = f"private_extract_foreign_entry: {part}"
                        raise RuntimeError(message) from None
                else:
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
                    self.created.add(self._key(os.fstat(child)))
                os.close(current)
                current = child
        except BaseException:
            os.close(current)
            raise
        return current

    def add(self, archive: tarfile.TarFile, member: tarfile.TarInfo, dest: str) -> None:
        if (
            member.name.startswith("/")
            or ".." in PurePosixPath(member.name).parts
            or not member.name.strip("./")
        ):
            message = f"private_extract_unsafe_member: {member.name}"
            raise RuntimeError(message)
        member = tarfile.data_filter(member, dest)
        parts = PurePosixPath(member.name).parts
        if member.isdir():
            os.close(self._descend(parts))
            return
        parent = self._descend(parts[:-1])
        try:
            if member.isreg():
                output = os.open(
                    parts[-1], FILE_FLAGS, member.mode & 0o777, dir_fd=parent
                )
                try:
                    self.created.add(self._key(os.fstat(output)))
                    source = archive.extractfile(member)
                    if source is None:
                        message = f"private_extract_unsupported_member: {member.name}"
                        raise RuntimeError(message)
                    with os.fdopen(output, "wb", closefd=False) as sink:
                        shutil.copyfileobj(source, sink)
                finally:
                    os.close(output)
            elif member.issym():
                os.symlink(member.linkname, parts[-1], dir_fd=parent)
                self.created.add(
                    self._key(os.lstat(parts[-1], dir_fd=parent)),
                )
            else:
                message = f"private_extract_unsupported_member: {member.name}"
                raise RuntimeError(message)
        finally:
            os.close(parent)

    def verify(self) -> None:
        """Every entry now under the subdirectory must be one this run created."""
        found = {self._key(os.fstat(self.top))}
        pending = [os.dup(self.top)]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        identity = os.lstat(entry.name, dir_fd=directory)
                        found.add(self._key(identity))
                        if stat.S_ISDIR(identity.st_mode):
                            pending.append(
                                os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory)
                            )
            finally:
                os.close(directory)
        if found != self.created:
            message = "private_extract_foreign_entry"
            raise RuntimeError(message)


def extract_owned(root: int, expected: str, name: str, stream: IO[bytes]) -> str:
    """Create ``name`` fresh and unpack a tar stream into it; return its identity."""
    _require_directory(root, expected)
    _require_child_name(name)
    # Opening the stream waits for the first header, so the subdirectory is
    # created only once the producer is already running.
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        os.mkdir(name, PRIVATE_MODE, dir_fd=root)
        top = os.open(name, DIRECTORY_FLAGS, dir_fd=root)
        try:
            extractor = _Extractor(top)
            dest = os.path.realpath(f"/proc/self/fd/{top}")
            for member in archive:
                extractor.add(archive, member, dest)
            extractor.verify()
            return _identity(top)
        finally:
            os.close(top)


def write_replacing(path: Path, text: str) -> None:
    """Publish a rewritten receipt without following or truncating a foreign name."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(text)
    temporary.replace(path)


def _main(arguments: list[str]) -> None:
    command = arguments[0]
    if command == "unlink":
        unlink_owned(
            int(arguments[1]),
            arguments[2],
            arguments[3],
            missing_ok=arguments[4] == "1",
        )
    elif command == "mkdir":
        for child in arguments[3:]:
            identity = mkdir_owned(int(arguments[1]), arguments[2], child)
            sys.stdout.write(f"{child} {identity}\n")
    elif command == "extract":
        identity = extract_owned(
            int(arguments[1]), arguments[2], arguments[3], sys.stdin.buffer
        )
        sys.stdout.write(f"{arguments[3]} {identity}\n")
    elif command == "symlink":
        symlink_owned(arguments[1], arguments[2], arguments[3], arguments[4])
    elif command == "empty":
        require_empty(arguments[1], arguments[2])
    elif command == "check":
        check_tree(
            int(arguments[1]),
            arguments[2],
            dict(item.split("=", 1) for item in arguments[3:]),
        )
    else:
        create(command)


if __name__ == "__main__":
    _main(sys.argv[1:])
