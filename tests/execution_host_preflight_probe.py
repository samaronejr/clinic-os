from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

type PathArgument = str | bytes | os.PathLike[str] | os.PathLike[bytes]
type StatArgument = int | PathArgument
type Checkpoint = Callable[[str], None]


class HarnessPaths(Protocol):
    authority_root: Path
    artifact: Path
    pending: Path
    stable_lock: Path


_OS_OPEN: Final = os.open
_OS_CLOSE: Final = os.close
_OS_DUP: Final = os.dup
_OS_WRITE: Final = os.write
_OS_FSYNC: Final = os.fsync
_OS_FCHMOD: Final = os.fchmod
_OS_LINK: Final = os.link
_OS_UNLINK: Final = os.unlink
_OS_STAT: Final = os.stat
_OS_FSTAT: Final = os.fstat


@dataclass(slots=True)
class EventProbe:
    harness: HarnessPaths
    checkpoint: Checkpoint | None
    descriptor_paths: dict[int, Path] = field(default_factory=dict)
    emitted: set[str] = field(default_factory=set)
    linked_pending_stats: int = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "close", self.close)
        monkeypatch.setattr(os, "dup", self.dup)
        monkeypatch.setattr(os, "write", self.write)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(os, "fchmod", self.fchmod)
        monkeypatch.setattr(os, "link", self.link)
        monkeypatch.setattr(os, "unlink", self.unlink)
        monkeypatch.setattr(os, "stat", self.stat)
        monkeypatch.setattr(os, "fstat", self.fstat)

    def open(
        self,
        path: PathArgument,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = (
            _OS_OPEN(path, flags, mode)
            if dir_fd is None
            else _OS_OPEN(path, flags, mode, dir_fd=dir_fd)
        )
        normalized = Path(os.fsdecode(path))
        self.descriptor_paths[descriptor] = normalized
        if normalized == self.harness.pending:
            if flags & os.O_CREAT and flags & os.O_EXCL:
                self.emit("pending-created")
            elif flags & os.O_RDWR:
                self.emit("pending-writable-reopen")
        return descriptor

    def close(self, descriptor: int) -> None:
        self.descriptor_paths.pop(descriptor, None)
        _OS_CLOSE(descriptor)

    def dup(self, descriptor: int) -> int:
        duplicate = _OS_DUP(descriptor)
        self.descriptor_paths[duplicate] = self.descriptor_paths.get(
            descriptor,
            self.harness.stable_lock,
        )
        return duplicate

    def write(self, descriptor: int, data: bytes | bytearray | memoryview[int]) -> int:
        written = _OS_WRITE(descriptor, data)
        if self.descriptor_paths.get(descriptor) == self.harness.pending:
            self.emit("pending-reconstructed")
        return written

    def fsync(self, descriptor: int) -> None:
        _OS_FSYNC(descriptor)
        path = self.descriptor_paths.get(descriptor)
        if path == self.harness.pending:
            mode = stat.S_IMODE(_OS_FSTAT(descriptor).st_mode)
            self.emit(
                "pending-metadata-synced" if mode == 0o400 else "pending-data-synced"
            )
        elif path == self.harness.authority_root:
            if self.harness.artifact.exists() and self.harness.pending.exists():
                self.emit("proof-directory-synced")
            elif self.harness.artifact.exists():
                self.emit("cleanup-directory-synced")
            else:
                self.emit("pending-directory-synced")

    def fchmod(self, descriptor: int, mode: int) -> None:
        _OS_FCHMOD(descriptor, mode)
        if (
            self.descriptor_paths.get(descriptor) == self.harness.pending
            and mode == 0o400
        ):
            self.emit("pending-mode-immutable")

    def link(
        self,
        source: PathArgument,
        destination: PathArgument,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        _OS_LINK(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if Path(os.fsdecode(destination)) == self.harness.artifact:
            self.emit("proof-linked")

    def unlink(self, path: PathArgument, *, dir_fd: int | None = None) -> None:
        if dir_fd is None:
            _OS_UNLINK(path)
        else:
            _OS_UNLINK(path, dir_fd=dir_fd)
        if Path(os.fsdecode(path)) == self.harness.pending:
            self.emit("pending-unlinked")

    def stat(
        self,
        path: StatArgument,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = _OS_STAT(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        normalized = (
            self.descriptor_paths.get(path)
            if isinstance(path, int)
            else Path(os.fsdecode(path))
        )
        if normalized == self.harness.pending and result.st_nlink == 2:
            self.linked_pending_stats += 1
            stage = (
                "linked-prefix-authenticated"
                if self.linked_pending_stats == 1
                else "linked-prefix-revalidated"
            )
            self.emit(stage)
        return result

    def fstat(self, descriptor: int) -> os.stat_result:
        result = _OS_FSTAT(descriptor)
        if (
            self.descriptor_paths.get(descriptor) == self.harness.pending
            and result.st_nlink == 2
        ):
            self.emit("linked-descriptor-authenticated")
        return result

    def emit(self, stage: str) -> None:
        if stage in self.emitted:
            return
        self.emitted.add(stage)
        if self.checkpoint is not None:
            self.checkpoint(stage)

    def close_leaked_descriptors(self) -> None:
        for descriptor in tuple(self.descriptor_paths):
            try:
                self.close(descriptor)
            except OSError:
                self.descriptor_paths.pop(descriptor, None)
