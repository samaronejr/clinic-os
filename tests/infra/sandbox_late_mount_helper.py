"""Mount something on the host side after a sandboxed child has started.

Run inside ``unshare --user --map-root-user --mount --propagation shared`` so
the shared directory is a peer group the sandbox could receive events from. The
child announces that it runs, waits on a pipe, and only then does this helper
mount a tmpfs at a path the child can name; the child reports what it sees.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

MOUNT = shutil.which("mount") or "/usr/bin/mount"
CHILD = """
import os, sys
print("ready", flush=True)
sys.stdin.readline()
late = sys.argv[1] + "/late"
try:
    print("SEEN", sorted(os.listdir(late)), flush=True)
except OSError as error:
    print("SEEN missing", error.errno, flush=True)
# The reviewer's route: a link inside the run directory to a path the host
# mounts over later, then a write through that link.
planted = sys.argv[2] + "/planted"
os.symlink(late + "/target", planted)
try:
    open(planted, "w").write("PWN")
    print("WROTE", flush=True)
except OSError as error:
    print("NOT-WRITTEN", error.errno, flush=True)
"""


def main(arguments: list[str]) -> None:
    shared, sandbox, run_fd, name, read_only, python = arguments
    subprocess.run([MOUNT, "--bind", shared, shared], check=True)  # noqa: S603 - fixed
    subprocess.run([MOUNT, "--make-shared", shared], check=True)  # noqa: S603 - fixed
    child = subprocess.Popen(  # noqa: S603 - fixed sandbox launcher
        [
            python,
            sandbox,
            str(os.getpid()),
            run_fd,
            name,
            "--",
            python,
            "-I",
            "-c",
            CHILD,
            shared,
            f"/dev/shm/{name}",  # noqa: S108 - the run mount inside the sandbox
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd="/usr",
        pass_fds=(int(run_fd),),
        env={
            **os.environ,
            "UPGRADE_SANDBOX_READ_ONLY": read_only,
            "UPGRADE_SANDBOX_STDIN": "pipe",
        },
    )
    assert child.stdout is not None
    assert child.stdin is not None
    assert child.stdout.readline().strip() == "ready"
    late = Path(shared) / "late"
    late.mkdir()
    subprocess.run([MOUNT, "-t", "tmpfs", "tmpfs", str(late)], check=True)  # noqa: S603 - fixed
    (late / "target").write_text("secret")
    child.stdin.write("go\n")
    child.stdin.close()
    sys.stdout.write(child.stdout.read())
    child.wait()
    sys.stdout.write(f"HOST-TARGET {(late / 'target').read_text()}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
