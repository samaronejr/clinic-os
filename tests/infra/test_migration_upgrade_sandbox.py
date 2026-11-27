"""A rehearsal child reaches nothing writable except the private run directory."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from ops.testing.migration_upgrade_custody import interpreter_prefixes

SANDBOX = (
    Path(__file__).resolve().parents[2] / "ops/testing/migration_upgrade_sandbox.py"
)
BWRAP = shutil.which("bwrap") or "bwrap"
# The child needs its interpreter, as the wrapper's children need the project.
READ_ONLY_ENV = {
    "UPGRADE_SANDBOX_READ_ONLY": os.pathsep.join(
        {
            str(Path(sys.executable).parent.parent),
            str(Path(sys.base_prefix).resolve()),
            *interpreter_prefixes(sys.executable),
        }
    )
}
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
        read_only: tuple[Path, ...] = (),
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
            cwd=cwd or "/usr",
            env={
                **(env or os.environ),
                "UPGRADE_SANDBOX_READ_ONLY": os.pathsep.join(
                    [READ_ONLY_ENV["UPGRADE_SANDBOX_READ_ONLY"], *map(str, read_only)]
                ),
            },
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
    # The foreign path is not mounted at all, or is mounted read-only.
    assert any(
        e in result.stderr for e in ("OSError", "FileNotFoundError", "PermissionError")
    )
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
        cwd="/usr",
        env={**os.environ, **READ_ONLY_ENV},
    )

    assert result.returncode != 0
    assert any(e in result.stderr for e in ("Read-only file system", "No such file"))
    assert victim.read_bytes() == b"foreign\n"


UNSHARE = shutil.which("unshare") or "/usr/bin/unshare"
HELPER = Path(__file__).resolve().parent / "sandbox_late_mount_helper.py"


def test_a_host_mount_made_after_the_child_starts_is_neither_visible_nor_writable(
    run: Run, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    read_only = os.pathsep.join(
        [READ_ONLY_ENV["UPGRADE_SANDBOX_READ_ONLY"], str(shared), str(HELPER.parent)]
    )

    result = subprocess.run(  # noqa: S603 - fixed helper in a private namespace
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "shared",
            sys.executable,
            str(HELPER),
            str(shared),
            str(SANDBOX),
            str(run.run_fd),
            NAME,
            read_only,
            sys.executable,
        ],
        pass_fds=(run.run_fd,),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # A mount the host made after the child started never reaches the child: it
    # does not see the host's file, cannot write through a link to it, and the
    # host's file is exactly what the host put there.
    assert "SEEN ['target']" not in result.stdout, result.stdout
    assert "WROTE" not in result.stdout, result.stdout
    assert "NOT-WRITTEN" in result.stdout, result.stdout
    assert "HOST-TARGET secret" in result.stdout, result.stdout


def test_links_to_inherited_stdio_never_reopen_the_hosts_files(
    run: Run, tmp_path: Path
) -> None:
    prefix = b"HOST-FILE\n"
    host = {name: tmp_path / name for name in ("stdin", "stdout", "stderr")}
    for path in host.values():
        path.write_bytes(prefix)
    for number in range(3):
        (run.root / f"proc{number}").symlink_to(f"/proc/self/fd/{number}")
        (run.root / f"dev{number}").symlink_to(f"/dev/fd/{number}")
    code = (
        "import sys\n"
        "for kind in ('proc', 'dev'):\n"
        "    for number in range(3):\n"
        "        try:\n"
        f"            open(f'{INSIDE}/{{kind}}{{number}}', 'w').write('PWN')\n"
        "        except OSError as error:\n"
        "            print('refused', kind, number, error.errno, file=sys.stderr)\n"
    )
    with (
        host["stdin"].open("rb") as stdin,
        host["stdout"].open("ab") as stdout,
        host["stderr"].open("ab") as stderr,
    ):
        result = subprocess.run(  # noqa: S603 - fixed launcher under test
            [
                sys.executable,
                str(SANDBOX),
                str(run.pid),
                str(run.run_fd),
                NAME,
                f"{run.entry_fd}:tmp",
                "--",
                sys.executable,
                "-I",
                "-c",
                code,
            ],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=(run.run_fd, run.entry_fd),
            cwd="/usr",
            env={**os.environ, **READ_ONLY_ENV},
            check=False,
            timeout=60,
        )

    assert result.returncode == 0
    # Nothing the child wrote could reopen, truncate or overwrite a host file.
    assert host["stdin"].read_bytes() == prefix
    for stream in ("stdout", "stderr"):
        assert host[stream].read_bytes().startswith(prefix), stream


ESCAPE_HELPER = Path(__file__).resolve().parent / "sandbox_userns_escape_helper.py"
NO_LANDLOCK_HELPER = (
    Path(__file__).resolve().parent / "sandbox_without_landlock_helper.py"
)


def test_a_same_user_process_cannot_lift_the_sandbox_by_clearing_a_mount_flag(
    run: Run, foreign: tuple[Path, Path], tmp_path: Path
) -> None:
    """The architect's escape: enter the sandbox's namespaces, clear MS_RDONLY."""
    _, marker = foreign
    (run.root / "planted").symlink_to(marker)
    code = (
        "import sys\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('PWN')\n"
        "    print('WROTE', flush=True)\n"
        "except OSError as error:\n"
        "    print('REFUSED', error.errno, flush=True)\n"
    )
    read_only = os.pathsep.join(
        [READ_ONLY_ENV["UPGRADE_SANDBOX_READ_ONLY"], str(tmp_path)]
    )
    command = [
        sys.executable,
        str(SANDBOX),
        str(run.pid),
        str(run.run_fd),
        NAME,
        "--",
        sys.executable,
        "-I",
        "-c",
        code,
        f"{INSIDE}/planted",
    ]
    environment = {
        **os.environ,
        "UPGRADE_SANDBOX_READ_ONLY": read_only,
        "UPGRADE_SANDBOX_STDIN": "pipe",
    }
    with subprocess.Popen(  # noqa: S603 - fixed launcher under test
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd="/usr",
        pass_fds=(run.run_fd,),
        env=environment,
    ) as child:
        assert child.stdout is not None
        assert child.stdin is not None
        assert child.stdout.readline().strip() == "READY"
        attack = subprocess.run(  # noqa: S603 - fixed helper acting as the peer
            [sys.executable, str(ESCAPE_HELPER), str(child.pid), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        # The attack must have reached the flag, or the test proves nothing.
        assert attack.stdout.strip() == "CLEARED", attack.stdout + attack.stderr
        child.stdin.write("GO\n")
        child.stdin.close()
        outcome = child.stdout.read()

    assert "WROTE" not in outcome, outcome
    assert "REFUSED" in outcome, outcome
    assert marker.read_bytes() == b"foreign\n"


def test_a_kernel_without_landlock_refuses_to_run_the_child(run: Run) -> None:
    result = subprocess.run(  # noqa: S603 - fixed launcher under test
        [
            sys.executable,
            str(NO_LANDLOCK_HELPER),
            str(SANDBOX),
            str(run.pid),
            str(run.run_fd),
            NAME,
            "--",
            sys.executable,
            "-I",
            "-c",
            f"open('{INSIDE}/ran', 'w').write('x')",
        ],
        pass_fds=(run.run_fd,),
        capture_output=True,
        text=True,
        check=False,
        cwd="/usr",
        env={**os.environ, **READ_ONLY_ENV},
    )

    assert result.returncode == 125, result.stderr
    assert "sandbox_landlock_unavailable" in result.stderr
    assert not (run.root / "ran").exists()


def test_a_child_is_given_no_host_service_to_delegate_to(
    run: Run, tmp_path: Path
) -> None:
    # A pathname socket must fit in 107 bytes, which pytest's long temp roots do not.
    directory = Path(tempfile.mkdtemp(prefix="ll"))
    service = directory / "s.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(service))
    server.listen(1)
    code = (
        "import os, socket, sys\n"
        "scrubbed = ('DOCKER', 'SSH', 'DBUS', 'XDG_RUNTIME', 'DISPLAY')\n"
        "keys = sorted(k for k in os.environ if k.startswith(scrubbed))\n"
        "print('ENV', keys)\n"
        "print('DOCKER_SOCKET', os.path.exists('/run/docker.sock'))\n"
        "print('FDS', sorted(int(n) for n in os.listdir('/proc/self/fd')))\n"
        "try:\n"
        "    socket.socket(socket.AF_UNIX).connect(sys.argv[1])\n"
        "    print('CONNECTED')\n"
        "except OSError as error:\n"
        "    print('CONNECT', type(error).__name__)\n"
    )
    environment = {
        **os.environ,
        "DOCKER_HOST": "unix:///run/docker.sock",
        "SSH_AUTH_SOCK": str(service),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={service}",
        "XDG_RUNTIME_DIR": str(directory),
        "DISPLAY": ":0",
    }
    try:
        result = run.python(code, str(service), env=environment, read_only=(directory,))
    finally:
        server.close()
        shutil.rmtree(directory)

    assert result.returncode == 0, result.stderr
    assert "ENV []" in result.stdout, result.stdout
    assert "DOCKER_SOCKET False" in result.stdout, result.stdout
    assert "FDS [0, 1, 2, 3]" in result.stdout, result.stdout
    assert "CONNECT PermissionError" in result.stdout, result.stdout


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A same-user process that enters the sandbox's namespaces can mount a "
        "foreign directory under the run directory, and a path-based policy then "
        "counts it as inside. Only a different principal, a protected runner, "
        "closes this; the claim records it."
    ),
)
def test_a_same_user_process_cannot_mount_a_foreign_directory_into_the_run(
    run: Run, tmp_path: Path
) -> None:
    victim_directory = tmp_path / "victim-directory"
    victim_directory.mkdir()
    victim = victim_directory / "victim"
    victim.write_bytes(b"ORIGINAL")
    (run.root / "x").mkdir()
    code = (
        "import sys\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('PWN')\n"
        "except OSError as error:\n"
        "    print('REFUSED', error.errno, flush=True)\n"
    )
    environment = {
        **os.environ,
        "UPGRADE_SANDBOX_READ_ONLY": os.pathsep.join(
            [READ_ONLY_ENV["UPGRADE_SANDBOX_READ_ONLY"], str(tmp_path)]
        ),
        "UPGRADE_SANDBOX_STDIN": "pipe",
    }
    command = [
        sys.executable,
        str(SANDBOX),
        str(run.pid),
        str(run.run_fd),
        NAME,
        "--",
        sys.executable,
        "-I",
        "-c",
        code,
        f"{INSIDE}/x/victim",
    ]
    with subprocess.Popen(  # noqa: S603 - fixed launcher under test
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd="/usr",
        pass_fds=(run.run_fd,),
        env=environment,
    ) as child:
        assert child.stdout is not None
        assert child.stdin is not None
        assert child.stdout.readline().strip() == "READY"
        attack = subprocess.run(  # noqa: S603 - fixed helper acting as the peer
            [
                sys.executable,
                str(ESCAPE_HELPER),
                str(child.pid),
                f"{INSIDE}/x",
                str(victim_directory),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert attack.stdout.strip() == "CLEARED", attack.stdout + attack.stderr
        child.stdin.write("GO\n")
        child.stdin.close()
        child.stdout.read()

    assert victim.read_bytes() == b"ORIGINAL"
