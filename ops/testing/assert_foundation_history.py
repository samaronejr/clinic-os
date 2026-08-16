"""Prove every migration present at the foundation SHA remains byte-identical."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Final, Never

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.isolation_common import IsolationError, JsonObject, canonical_bytes

SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
GIT: Final = shutil.which("git")
ARGUMENT_COUNT: Final = 4


def _fail(message: str) -> Never:
    raise IsolationError(message)


def assert_foundation_history(worktree: Path, foundation_sha: str) -> JsonObject:
    """Return a digest record only when every foundation migration is unchanged."""
    root = _worktree(worktree)
    if SHA_PATTERN.fullmatch(foundation_sha) is None:
        _fail("foundation SHA must be lowercase 40-hex")
    _git(root, "cat-file", "-e", f"{foundation_sha}^{{commit}}")
    names = _git(root, "ls-tree", "-r", "-z", "--name-only", foundation_sha)
    paths = sorted(
        (PurePosixPath(os.fsdecode(raw)) for raw in names.split(b"\0") if raw),
        key=lambda path: path.as_posix().encode(),
    )
    migrations = [path for path in paths if _is_migration(path)]
    if not migrations:
        _fail("foundation commit contains no migration files")
    digest = hashlib.sha256()
    for relative in migrations:
        path = root / relative
        value = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode):
            _fail(f"foundation migration is not a regular file: {relative}")
        expected = _git(root, "show", f"{foundation_sha}:{relative.as_posix()}")
        observed = path.read_bytes()
        if observed != expected:
            _fail(f"foundation migration bytes changed: {relative}")
        encoded = relative.as_posix().encode()
        digest.update(encoded)
        digest.update(b"\0")
        digest.update(len(observed).to_bytes(8, "big"))
        digest.update(observed)
    return {
        "foundation_sha": foundation_sha,
        "migration_count": len(migrations),
        "migrations_sha256": digest.hexdigest(),
        "schema_version": 1,
    }


def _worktree(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        _fail("worktree must be an absolute non-symlink directory")
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        _fail("worktree path is noncanonical")
    return resolved


def _is_migration(path: PurePosixPath) -> bool:
    return (
        path.suffix == ".py" and "migrations" in path.parts and path.parts[0] == "apps"
    )


def _git(worktree: Path, *arguments: str) -> bytes:
    if GIT is None:
        _fail("git executable is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved Git binary and tuple argv.
        (GIT, "-C", worktree, *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        _fail("git rejected the foundation-history query")
    return result.stdout


def _main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) == 1:
        foundation_sha = arguments[0]
        worktree = Path.cwd()
    elif len(arguments) == ARGUMENT_COUNT and arguments[0::2] == [
        "--foundation-sha",
        "--worktree",
    ]:
        foundation_sha = arguments[1]
        worktree = Path(arguments[3])
    else:
        sys.stderr.write("assert-foundation-history: invalid command grammar\n")
        return 2
    try:
        result = assert_foundation_history(worktree, foundation_sha)
    except (IsolationError, FileNotFoundError, PermissionError, OSError) as error:
        sys.stderr.write(f"assert-foundation-history: {error}\n")
        return 2
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
