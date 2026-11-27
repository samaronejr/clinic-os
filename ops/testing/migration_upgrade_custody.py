"""Create and use the rehearsal's private run directory only through its descriptor."""

import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

PRIVATE_MODE = 0o700
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
HELPER_TREES = ("ops/testing", "ops/db")
RECORD_SEPARATOR = "\x1e"


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


@dataclass(frozen=True)
class _Census:
    """What a census of the run tree compares each entry against."""

    root_device: int
    root_path: str
    children: dict[str, str]
    recorded: dict[str, str]
    allowed: frozenset[tuple[int, int]]
    # (dev, ino) -> [st_nlink, names seen in the tree, one path for the report]
    names: dict[tuple[int, int], list[object]] = field(default_factory=dict)


def _link_resolves_inside(directory: int, name: str, census: _Census) -> bool:
    """Judge a link by what it resolves to, never by what its target is named.

    It may resolve to an inode recorded when the run started, or to a path
    under the run directory on the run's own device. A link into ``/proc`` or
    ``/dev`` resolves to the wrapper's own descriptors and is refused, even
    when the file behind it happens to share the run's device.
    """
    try:
        found = os.stat(name, dir_fd=directory)
        resolved = os.path.realpath(f"/proc/self/fd/{directory}/{name}")
    except OSError:
        return False
    if (found.st_dev, found.st_ino) in census.allowed:
        return True
    inside = resolved == census.root_path or resolved.startswith(f"{census.root_path}/")
    return found.st_dev == census.root_device and inside


def _census_entry(
    directory: int, prefix: str, name: str, census: _Census
) -> tuple[int, str] | None:
    """Judge one entry; return the directory to descend into, if any."""
    relative = prefix + name
    found = os.lstat(name, dir_fd=directory)
    if found.st_dev != census.root_device:
        message = f"private_tree_foreign_mount: {relative}"
        raise RuntimeError(message)
    children = census.children
    if (
        prefix == ""
        and name in children
        and (
            not stat.S_ISDIR(found.st_mode)
            or f"{found.st_dev}:{found.st_ino}" != children[name]
        )
    ):
        message = f"private_tree_replaced: {name}"
        raise RuntimeError(message)
    if stat.S_ISDIR(found.st_mode):
        return os.open(name, DIRECTORY_FLAGS, dir_fd=directory), relative + "/"
    if stat.S_ISLNK(found.st_mode):
        # Judged by what it resolves to, wherever it sits and whatever it is called.
        allowed_link = census.recorded.get(relative) == os.readlink(
            name, dir_fd=directory
        ) or _link_resolves_inside(directory, name, census)
    else:
        allowed_link = stat.S_ISREG(found.st_mode)
        if allowed_link:
            seen = census.names.setdefault(
                (found.st_dev, found.st_ino), [found.st_nlink, 0, relative]
            )
            seen[1] = int(seen[1]) + 1  # type: ignore[call-overload]
    if not allowed_link:
        message = f"private_tree_foreign_entry: {relative}"
        raise RuntimeError(message)
    return None


def check_tree(
    root: int,
    expected: str,
    children: dict[str, str],
    links: dict[str, str] | None = None,
    allowed: frozenset[tuple[int, int]] = frozenset(),
) -> None:
    """Census the whole run tree against what the run created.

    Refuses a missing or replaced created child, any link the run did not
    record that does not resolve to the run's own device or to an inode
    recorded at the start, any entry on another device, and any entry that is
    not a file, a directory or such a link.
    """
    _require_directory(root, expected)
    judged = _Census(
        os.fstat(root).st_dev,
        str(Path(f"/proc/self/fd/{root}").readlink()),
        children,
        links or {},
        allowed,
    )
    pending = [(os.dup(root), "")]
    top_level: set[str] = set()
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if prefix == "":
                        top_level.add(entry.name)
                    descend = _census_entry(directory, prefix, entry.name, judged)
                    if descend is not None:
                        pending.append(descend)
        finally:
            os.close(directory)
    if not set(children) <= top_level:
        message = "private_tree_missing_entry"
        raise RuntimeError(message)
    # Every name an inode has must be inside this tree: an inode with a name
    # outside it is a foreign file that was linked in, wherever it sits.
    for nlink, seen, relative in judged.names.values():
        if nlink != seen:
            message = f"private_tree_foreign_entry: {relative}"
            raise RuntimeError(message)


def _digest(directory: int, name: str) -> str:
    """Hash a regular file read through its directory descriptor, never by path."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    held = os.open(name, flags, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(held).st_mode):
            message = "private_tree_foreign_entry"
            raise RuntimeError(message)
        digest = hashlib.sha256()
        while block := os.read(held, 1 << 20):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(held)


def snapshot(root: int, expected: str) -> dict[str, list[object]]:
    """Record every object in the run tree: identity, link count and content.

    The record is taken from outside every child, after the census accepted the
    tree, so a later comparison shows exactly what changed since this stage.
    """
    _require_directory(root, expected)
    entries: dict[str, list[object]] = {}
    pending = [(os.dup(root), "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as scanned:
                for entry in scanned:
                    found = os.lstat(entry.name, dir_fd=directory)
                    relative = prefix + entry.name
                    kind, detail = "d", ""
                    if stat.S_ISREG(found.st_mode):
                        kind, detail = "f", _digest(directory, entry.name)
                    elif stat.S_ISLNK(found.st_mode):
                        kind, detail = "l", os.readlink(entry.name, dir_fd=directory)
                    elif stat.S_ISDIR(found.st_mode):
                        pending.append(
                            (
                                os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory),
                                relative + "/",
                            )
                        )
                    else:
                        message = f"private_tree_foreign_entry: {relative}"
                        raise RuntimeError(message)
                    entries[relative] = [
                        kind,
                        found.st_dev,
                        found.st_ino,
                        found.st_nlink,
                        detail,
                    ]
        finally:
            os.close(directory)
    return entries


def unchanged_since(
    recorded: dict[str, list[object]],
    current: dict[str, list[object]],
    new_names: frozenset[str] = frozenset(),
) -> None:
    """Refuse an object missing, new, replaced or rewritten since the record."""
    for relative, found in current.items():
        if relative not in recorded:
            if relative not in new_names:
                message = f"private_tree_changed: new {relative}"
                raise RuntimeError(message)
        elif recorded[relative] != found:
            message = f"private_tree_changed: {relative}"
            raise RuntimeError(message)
    for relative in recorded:
        if relative not in current:
            message = f"private_tree_changed: missing {relative}"
            raise RuntimeError(message)


class _Extractor:
    """Unpack a tar stream into a directory this run created, never by name."""

    def __init__(self, top: int, links: dict[str, str]) -> None:
        self.top = top
        self.links = links
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
            or any(c in member.name + member.linkname for c in "\t\n")
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
                self.links[posixpath.join(*parts)] = member.linkname
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


def extract_owned(
    root: int,
    expected: str,
    name: str,
    stream: IO[bytes],
    links: dict[str, str] | None = None,
) -> str:
    """Create ``name`` fresh and unpack a tar stream into it; return its identity.

    The links the archive carried are added to ``links`` by path under ``name``.
    """
    members: dict[str, str] = {}
    _require_directory(root, expected)
    _require_child_name(name)
    # Opening the stream waits for the first header, so the subdirectory is
    # created only once the producer is already running.
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        os.mkdir(name, PRIVATE_MODE, dir_fd=root)
        top = os.open(name, DIRECTORY_FLAGS, dir_fd=root)
        try:
            extractor = _Extractor(top, members)
            dest = os.path.realpath(f"/proc/self/fd/{top}")
            for member in archive:
                extractor.add(archive, member, dest)
            extractor.verify()
            if links is not None:
                links.update({f"{name}/{rel}": to for rel, to in members.items()})
            return _identity(top)
        finally:
            os.close(top)


def interpreter_prefixes(path: str) -> list[str]:
    """Each directory an interpreter's link chain passes through, to bind them.

    uv names a minor-version alias (``cpython-3.12-...``) that links to the
    patch-version install, so both prefixes are needed inside the sandbox.
    """
    prefixes: list[str] = []
    current = Path(path)
    for _ in range(16):
        if not current.is_symlink():
            break
        target = current.readlink()
        current = target if target.is_absolute() else current.parent / target
        prefixes.append(str(current.parent.parent))
    prefixes.append(str(current.resolve().parent.parent))
    return list(dict.fromkeys(prefixes))


def resolved_inode(path: str) -> str:
    """Return the ``dev:ino`` a path finally resolves to."""
    found = Path(path).stat()
    return f"{found.st_dev}:{found.st_ino}"


def write_replacing(path: Path, text: str) -> None:
    """Publish a rewritten receipt without following or truncating a foreign name."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(text)
    temporary.replace(path)


def capture_file(root: int, expected: str, name: str) -> bytes:
    """Read one run file exactly once, through the held run directory.

    The caller keeps the bytes; nothing that decides a verdict is read again by
    name afterwards.
    """
    _require_directory(root, expected)
    _require_child_name(name)
    held = os.open(name, READ_FLAGS, dir_fd=root)
    try:
        found = os.fstat(held)
        if (
            not stat.S_ISREG(found.st_mode)
            or found.st_nlink != 1
            or found.st_dev != os.fstat(root).st_dev
        ):
            message = f"private_capture_foreign_entry: {name}"
            raise RuntimeError(message)
        chunks = []
        while block := os.read(held, 1 << 20):
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(held)


def _git_object(project: str, kind: str, oid: str) -> bytes:
    """Read an object and prove its bytes hash to the name it was asked by."""
    git = shutil.which("git")
    if git is None:
        message = "helper_seal_git_missing"
        raise RuntimeError(message)
    data = subprocess.run(  # noqa: S603 - fixed git query
        [git, "-C", project, "cat-file", kind, oid],
        capture_output=True,
        check=True,
    ).stdout
    header = f"{kind} {len(data)}".encode() + b"\0"
    if hashlib.sha1(header + data, usedforsecurity=False).hexdigest() != oid:
        message = f"helper_seal_object_mismatch: {oid}"
        raise RuntimeError(message)
    return data


def _tree_entries(data: bytes) -> list[tuple[str, str, str]]:
    entries = []
    position = 0
    while position < len(data):
        space = data.index(b" ", position)
        end = data.index(b"\0", space)
        mode = data[position:space].decode()
        name = data[space + 1 : end].decode()
        entries.append((mode, name, data[end + 1 : end + 21].hex()))
        position = end + 21
    return entries


def _write_tree(project: str, oid: str, destination: Path) -> None:
    destination.mkdir(mode=PRIVATE_MODE, parents=True)
    for mode, name, child in _tree_entries(_git_object(project, "tree", oid)):
        _require_child_name(name)
        if mode == "40000":
            _write_tree(project, child, destination / name)
        elif mode in ("100644", "100755"):
            flags = FILE_FLAGS
            held = os.open(
                destination / name, flags, 0o500 if mode == "100755" else 0o400
            )
            with os.fdopen(held, "wb") as output:
                output.write(_git_object(project, "blob", child))
        else:
            message = f"helper_seal_unsupported_entry: {name}"
            raise RuntimeError(message)


def seal_helpers(project: str, commit: str, target: str) -> None:
    """Extract the harness helpers of ``commit`` into a fresh directory.

    Every object on the way from the commit to each file is checked against its
    own name, so the copy is the committed bytes whatever the checkout now holds.
    """
    commit_data = _git_object(project, "commit", commit)
    first = commit_data.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        message = "helper_seal_commit_unreadable"
        raise RuntimeError(message)
    top = first[5:].decode()
    Path(target).mkdir(mode=PRIVATE_MODE)
    for relative in HELPER_TREES:
        oid = top
        for part in PurePosixPath(relative).parts:
            found = [
                child
                for mode, name, child in _tree_entries(
                    _git_object(project, "tree", oid)
                )
                if name == part and mode == "40000"
            ]
            if not found:
                message = f"helper_seal_missing: {relative}"
                raise RuntimeError(message)
            oid = found[0]
        _write_tree(project, oid, Path(target) / relative)


def _file_sha256(path: Path) -> str | None:
    try:
        held = os.open(path, READ_FLAGS)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(held).st_mode):
            return None
        digest = hashlib.sha256()
        while block := os.read(held, 1 << 20):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(held)


def verify_helpers(project: str, sealed: str) -> None:
    """Refuse a checkout whose tracked helper differs from the sealed copy."""
    for tree in HELPER_TREES:
        for found in sorted((Path(sealed) / tree).rglob("*")):
            if found.is_dir() or "__pycache__" in found.parts:
                continue
            relative = found.relative_to(sealed)
            if _file_sha256(Path(project) / relative) != _file_sha256(found):
                message = f"helpers_changed: {relative}"
                raise RuntimeError(message)


def publish_verdict(arguments: list[str], held: str) -> None:
    """Publish a verdict built from the bytes the supervisor already holds.

    ``held`` is the verdict attestation and the differential report, in that
    order, split by a record separator; either may be empty. No run file is read.
    """
    (
        root,
        base,
        database,
        result,
        stage,
        run_id,
        commit,
        started,
        differential,
        nonce,
    ) = arguments
    attestation, _, report = held.partition(RECORD_SEPARATOR)
    target = Path(root) / "verdict.json"
    details = (
        json.loads(report)
        if report.strip()
        else {
            "result": "FAIL",
            "projection_cases": 0,
            "probe_window_cases": 0,
            "differences": [],
            "base_reject_lane_pass_pairs": [],
        }
    )
    if result == "PASS":
        verdict = json.loads(attestation)
        if differential and details.get("result") != "PASS":
            message = "differential passing report is missing"
            raise RuntimeError(message)
    else:
        verdict = {
            "schema_version": 1,
            "result": "FAIL",
            "base_sha": base,
            "upgrade_db": database,
            "reason": stage,
        }
    if differential:
        verdict["differential"] = details
    verdict.update(
        run_id=run_id, run_nonce=nonce, commit_sha=commit, started_at=started
    )
    pending = target.with_name(f".verdict-{secrets.token_hex(16)}.pending")
    with pending.open("x", encoding="utf-8") as stream:
        identity = os.fstat(stream.fileno())
        try:
            stream.write(json.dumps(verdict) + "\n")
            stream.flush()
            _require_staged(pending, identity)
            pending.replace(target)
        except BaseException:
            try:
                if os.path.samestat(pending.lstat(), identity):
                    with pending.open(encoding="utf-8") as receipt:
                        owned = os.path.samestat(os.fstat(receipt.fileno()), identity)
                        if (
                            owned
                            and json.load(receipt).get("run_nonce") == nonce
                            and os.path.samestat(pending.lstat(), identity)
                        ):
                            pending.unlink()
            except (OSError, ValueError):
                pass
            raise


def _require_staged(pending: Path, identity: os.stat_result) -> None:
    if not os.path.samestat(pending.lstat(), identity):
        message = "verdict staging file was replaced"
        raise RuntimeError(message)


def _check_command(arguments: list[str]) -> None:
    """Census the run tree: created children, recorded links, allowed inodes."""
    extra = ("link:", "inode:")
    recorded = [a[5:].split("\t", 1) for a in arguments[3:] if a.startswith("link:")]
    allowed = frozenset(
        (int(a[6:].split(":")[0]), int(a[6:].split(":")[1]))
        for a in arguments[3:]
        if a.startswith("inode:")
    )
    check_tree(
        int(arguments[1]),
        arguments[2],
        dict(a.split("=", 1) for a in arguments[3:] if not a.startswith(extra)),
        dict(recorded),
        allowed,
    )


def _interpreter_command(arguments: list[str]) -> None:
    """Print the prefixes an interpreter needs and the inode it resolves to."""
    for prefix in interpreter_prefixes(arguments[1]):
        sys.stdout.write(f"prefix {prefix}\n")
    sys.stdout.write(f"inode {resolved_inode(arguments[1])}\n")


def _inode_command(arguments: list[str]) -> None:
    sys.stdout.write(f"{resolved_inode(arguments[1])}\n")


def _snapshot_command(arguments: list[str]) -> None:
    """Print the accepted tree's record: custody.py snapshot RUN_FD IDENTITY.

    The caller holds the record in memory; no file carries it.
    """
    sys.stdout.write(json.dumps(snapshot(int(arguments[1]), arguments[2])))


def _unchanged_command(arguments: list[str]) -> None:
    """Compare the tree to the record on stdin, allowing the named new entries."""
    recorded = json.loads(sys.stdin.read())
    unchanged_since(
        recorded, snapshot(int(arguments[1]), arguments[2]), frozenset(arguments[3:])
    )


def _capture_command(arguments: list[str]) -> None:
    """Print one run file, read once: custody.py capture RUN_FD IDENTITY NAME."""
    sys.stdout.buffer.write(capture_file(int(arguments[1]), arguments[2], arguments[3]))


def _publish_command(arguments: list[str]) -> None:
    publish_verdict(arguments[1:], sys.stdin.read())


def _seal_helpers_command(arguments: list[str]) -> None:
    seal_helpers(arguments[1], arguments[2], arguments[3])


def _verify_helpers_command(arguments: list[str]) -> None:
    verify_helpers(arguments[1], arguments[2])


TOOLS = {
    "check": _check_command,
    "snapshot": _snapshot_command,
    "unchanged": _unchanged_command,
    "capture": _capture_command,
    "publish": _publish_command,
    "seal-helpers": _seal_helpers_command,
    "verify-helpers": _verify_helpers_command,
    "interpreter": _interpreter_command,
    "inode": _inode_command,
}


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
        links: dict[str, str] = {}
        identity = extract_owned(
            int(arguments[1]), arguments[2], arguments[3], sys.stdin.buffer, links
        )
        sys.stdout.write(f"{arguments[3]} {identity}\n")
        for rel, target in links.items():
            sys.stdout.write(f"link\t{rel}\t{target}\n")
    elif command == "symlink":
        symlink_owned(arguments[1], arguments[2], arguments[3], arguments[4])
    elif command == "empty":
        require_empty(arguments[1], arguments[2])
    elif command in TOOLS:
        TOOLS[command](arguments)
    else:
        create(command)


if __name__ == "__main__":
    _main(sys.argv[1:])
