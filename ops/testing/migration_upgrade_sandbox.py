"""Run one rehearsal child with the private run directory as its only writable mount.

Containment must not depend on a child tool declining to follow a name. The
child sees the host read-only, a fresh process namespace and ``/proc``, and a
single writable bind: the run directory, reached through the descriptor the
wrapper already holds. A planted link that resolves anywhere else fails with
EROFS or ENOENT.

Usage: migration_upgrade_sandbox.py WRAPPER_PID RUN_FD RUN_NAME [FD:ENTRY ...]
       -- COMMAND ...
RUN_FD and RUN_NAME are ``-`` when the child needs no run directory.
"""

import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

SHM = "/dev/shm"  # noqa: S108 - recreated as a private tmpfs inside the sandbox
SANDBOX_STUB = """
import json, os, stat, sys
spec = json.loads(sys.argv[1])
for path, identity in spec["identities"]:
    found = os.lstat(path)
    if not stat.S_ISDIR(found.st_mode) or f"{found.st_dev}:{found.st_ino}" != identity:
        sys.stderr.write(f"sandbox_entry_replaced: {path}\\n")
        raise SystemExit(125)
os.closerange(3, 1 << 20)
if spec["cwd"]:
    os.chdir(spec["cwd"][0])
    found = os.stat(".")
    if f"{found.st_dev}:{found.st_ino}" != spec["cwd"][1]:
        sys.stderr.write("sandbox_cwd_replaced\\n")
        raise SystemExit(125)
command = sys.argv[2:]
os.execvp(command[0], command)
"""


def _identity(descriptor: int) -> str:
    found = os.fstat(descriptor)
    return f"{found.st_dev}:{found.st_ino}"


def _translator(pid: str, mounts: dict[str, str]) -> Callable[[str], str]:
    pattern = re.compile(rf"/proc/{pid}/fd/(\d+)")

    def translate(text: str) -> str:
        def swap(match: re.Match[str]) -> str:
            if match[1] not in mounts:
                message = f"sandbox_unmapped_path: {match[0]}"
                raise RuntimeError(message)
            return mounts[match[1]]

        return pattern.sub(swap, text)

    return translate


def build(arguments: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return the launcher argv and the translated environment."""
    split = arguments.index("--")
    pid, run_fd, name, *entries = arguments[:split]
    command = arguments[split + 1 :]
    mounts: dict[str, str] = {}
    identities: list[tuple[str, str]] = []
    descriptors: dict[str, int] = {}
    mount_arguments: list[str] = []
    if run_fd != "-":
        if re.fullmatch(r"migration-upgrade-secrets\.[0-9a-f]{32}", name) is None:
            message = "sandbox_invalid_run_name"
            raise ValueError(message)
        root = f"{SHM}/{name}"
        mounts[run_fd] = root
        descriptors[root] = int(run_fd)
        mount_arguments = ["--bind", f"/proc/self/fd/{run_fd}", root]
        for entry in entries:
            descriptor, _, child = entry.partition(":")
            mounts[descriptor] = f"{root}/{child}"
            descriptors[f"{root}/{child}"] = int(descriptor)
            identities.append((f"{root}/{child}", _identity(int(descriptor))))
    translate = _translator(pid, mounts)
    environment = {key: translate(value) for key, value in os.environ.items()}
    translated = [translate(item) for item in command]
    here = Path().stat()
    cwd: list[str] = []
    for path, held in descriptors.items():
        if _identity(held) == f"{here.st_dev}:{here.st_ino}":
            cwd = [path, _identity(held)]
    if not cwd:
        cwd = [str(Path.cwd()), f"{here.st_dev}:{here.st_ino}"]
    launcher = shutil.which("bwrap")
    if launcher is None:
        message = "private_run_namespace_launcher_missing"
        raise RuntimeError(message)
    spec = json.dumps({"identities": identities, "cwd": cwd})
    argv = [
        launcher,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--tmpfs",
        SHM,
        *mount_arguments,
        "--remount-ro",
        SHM,
        "--proc",
        "/proc",
        "--",
        sys.executable,
        "-I",
        "-c",
        SANDBOX_STUB,
        spec,
        *translated,
    ]
    return argv, environment


def _close_unrelated(keep: set[int]) -> None:
    for entry in Path("/proc/self/fd").iterdir():
        descriptor = int(entry.name)
        if descriptor > 2 and descriptor not in keep:  # noqa: PLR2004
            try:
                os.close(descriptor)
            except OSError:
                continue


def main(arguments: list[str]) -> None:
    """Replace this process with the sandboxed child."""
    run_fd = arguments[1]
    argv, environment = build(arguments)
    keep = set() if run_fd == "-" else {int(run_fd)}
    # Only the run directory descriptor reaches the launcher's exec; the stub
    # closes even that before the child runs, so the child holds no handle
    # above the mount.
    _close_unrelated(keep)
    for descriptor in keep:
        os.set_inheritable(descriptor, True)  # noqa: FBT003 - positional-only API
    os.execve(argv[0], argv, environment)  # noqa: S606 - fixed sandbox launcher


if __name__ == "__main__":
    main(sys.argv[1:])
