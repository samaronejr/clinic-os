"""A rehearsal child reaches nothing writable except the private run directory."""

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SANDBOX = (
    Path(__file__).resolve().parents[2] / "ops/testing/migration_upgrade_sandbox.py"
)
BWRAP = shutil.which("bwrap") or "bwrap"
NAME = "migration-upgrade-secrets." + "a" * 32
INSIDE = f"/dev/shm/{NAME}"  # noqa: S108 - the sandbox's private mount point
TMP_INSIDE = f"{INSIDE}/tmp"  # noqa: S108 - an entry of that mount


class Run:
    def __init__(self, root: Path, run_fd: int, entry_fd: int) -> None:
        self.root = root
        self.run_fd = run_fd
        self.entry_fd = entry_fd
        self.pid = os.getpid()

    def path(self, descriptor: int, rest: str = "") -> str:
        return f"/proc/{self.pid}/fd/{descriptor}{rest}"

    def python(
        self,
        code: str,
        *arguments: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed launcher under test
            [
                sys.executable,
                str(SANDBOX),
                str(self.pid),
                str(self.run_fd),
                NAME,
                f"{self.entry_fd}:tmp",
                "--",
                sys.executable,
                "-I",
                "-c",
                code,
                *arguments,
            ],
            pass_fds=(self.run_fd, self.entry_fd),
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env=env,
        )


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    root = tmp_path / NAME
    root.mkdir(mode=0o700)
    (root / "tmp").mkdir()
    flags = os.O_RDONLY | os.O_DIRECTORY
    run_fd = os.open(root, flags)
    entry_fd = os.open(root / "tmp", flags)
    yield Run(root, run_fd, entry_fd)
    os.close(run_fd)
    os.close(entry_fd)


@pytest.fixture
def foreign(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "foreign-dir"
    directory.mkdir()
    marker = tmp_path / "foreign-file"
    marker.write_bytes(b"foreign\n")
    return directory, marker


def _unchanged(foreign: tuple[Path, Path]) -> None:
    directory, marker = foreign
    assert list(directory.iterdir()) == []
    assert marker.read_bytes() == b"foreign\n"


WRITE = "import sys\nopen(sys.argv[1], sys.argv[2]).write('PWN')\n"


def test_the_run_directory_is_the_one_writable_mount(run: Run) -> None:
    result = run.python(WRITE, f"{INSIDE}/created", "x")

    assert result.returncode == 0, result.stderr
    assert (run.root / "created").read_text(encoding="utf-8") == "PWN"


@pytest.mark.parametrize("kind", ["file", "directory", "proc_root", "dotdot"])
def test_links_and_escapes_never_reach_a_writable_foreign_inode(
    run: Run, foreign: tuple[Path, Path], kind: str
) -> None:
    directory, marker = foreign
    link = run.root / "link"
    if kind == "file":
        link.symlink_to(marker)
        target = f"{INSIDE}/link"
    elif kind == "directory":
        link.symlink_to(directory)
        target = f"{INSIDE}/link/pwned"
    elif kind == "proc_root":
        link.symlink_to(f"/proc/1/root{marker}")
        target = f"{INSIDE}/link"
    else:
        target = f"{INSIDE}/../escaped"

    result = run.python(WRITE, target, "w")

    assert result.returncode != 0
    assert "OSError" in result.stderr
    _unchanged(foreign)
    assert not (run.root.parent / "escaped").exists()


def test_nothing_outside_the_run_directory_is_writable(
    run: Run, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    result = run.python(WRITE, str(outside), "w")

    assert result.returncode != 0
    assert not outside.exists()


def test_held_paths_are_translated_into_the_mount(run: Run) -> None:
    code = (
        "import os, sys\n"
        "open(sys.argv[1], 'x').write(os.environ['HELD'])\n"
        "open('relative', 'x').write(os.getcwd())\n"
    )
    result = run.python(
        code,
        run.path(run.run_fd, "/new"),
        cwd=run.path(run.entry_fd),
        env={**os.environ, "HELD": run.path(run.entry_fd)},
    )

    assert result.returncode == 0, result.stderr
    assert (run.root / "new").read_text(encoding="utf-8") == TMP_INSIDE
    assert (run.root / "tmp/relative").read_text(encoding="utf-8") == TMP_INSIDE


def test_an_unmapped_descriptor_path_is_refused(run: Run) -> None:
    result = run.python("pass", run.path(0))

    assert result.returncode != 0
    assert "sandbox_unmapped_path" in result.stderr


def test_a_replaced_entry_is_refused_before_the_child_runs(run: Run) -> None:
    (run.root / "tmp").rename(run.root / "tmp.moved")
    (run.root / "tmp").mkdir()

    result = run.python(WRITE, f"{INSIDE}/ran", "x")

    assert result.returncode == 125
    assert "sandbox_entry_replaced" in result.stderr
    assert not (run.root / "ran").exists()


def test_the_child_sees_no_host_processes_and_holds_no_inherited_handles(
    run: Run,
) -> None:
    code = (
        "import os\n"
        "def live(n):\n"
        "    try:\n"
        "        os.fstat(n)\n"
        "    except OSError:\n"
        "        return False\n"
        "    return True\n"
        "print([n for n in range(3, 256) if live(n)])\n"
        "print(sorted(int(n) for n in os.listdir('/proc') if n.isdigit()))\n"
    )
    result = run.python(code)

    handles, processes = result.stdout.splitlines()
    assert handles == "[]", handles
    assert str(run.pid) not in processes


@pytest.mark.parametrize("mount", ["--ro-bind", "--bind"])
def test_a_host_submount_cannot_be_written_through_a_link(
    run: Run, tmp_path: Path, mount: str
) -> None:
    """A reviewer-owned directory mounted over the host stays read-only inside."""
    reviewer = tmp_path / "reviewer-owned"
    reviewer.mkdir()
    victim = reviewer / "target"
    victim.write_bytes(b"foreign\n")
    (run.root / "link").symlink_to(victim)

    result = subprocess.run(  # noqa: S603 - fixed launcher under test
        [
            BWRAP,
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            mount,
            str(reviewer),
            str(reviewer),
            "--",
            sys.executable,
            str(SANDBOX),
            str(run.pid),
            str(run.run_fd),
            NAME,
            "--",
            sys.executable,
            "-I",
            "-c",
            WRITE,
            f"{INSIDE}/link",
            "w",
        ],
        pass_fds=(run.run_fd,),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Read-only file system" in result.stderr, result.stderr
    assert victim.read_bytes() == b"foreign\n"
