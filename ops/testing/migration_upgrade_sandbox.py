"""Run one rehearsal child with the private run directory as its only writable mount.

Containment must not depend on a child tool declining to follow a name. The
child sees only the exact host paths it needs, read-only, a fresh process
namespace and ``/proc``, and a single writable bind: the run directory,
reached through the descriptor the wrapper already holds.

The bind mounts are hygiene. The guarantee is a Landlock ruleset the kernel
applies to the child before it runs (``migration_upgrade_landlock.py``): write,
create, remove, rename and truncate only beneath the run directory, whose
descriptor the wrapper already holds, and read and execute only the listed
inputs. It is attached to the child's own credentials, so a process of the same
user that enters the sandbox's namespaces and clears a mount flag still cannot
lift it. A kernel that cannot apply it is refused by name; there is no fallback.

Three more properties close the routes a mounted host or an inherited
descriptor would otherwise leave open:

* the launcher first enters a mount namespace whose mounts are all private, so
  a mount made on the host after the child starts never appears inside it;
* the child's standard streams are pipes this launcher relays (stdin is
  ``/dev/null`` unless the caller asks for a pipe), never the wrapper's host
  files, so ``/proc/self/fd/N`` cannot reopen one for writing;
* every other descriptor is closed before the child runs.

Usage: migration_upgrade_sandbox.py WRAPPER_PID RUN_FD RUN_NAME [FD:ENTRY ...]
       -- COMMAND ...
RUN_FD and RUN_NAME are ``-`` when the child needs no run directory. The
environment names the extra read-only paths (``UPGRADE_SANDBOX_READ_ONLY``,
separated by ``os.pathsep``) and asks for a stdin pipe
(``UPGRADE_SANDBOX_STDIN=pipe``).
"""

import ctypes
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType, ModuleType

SHM = "/dev/shm"  # noqa: S108 - recreated as a private tmpfs inside the sandbox
SYSTEM_READ_ONLY = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/run/systemd/resolve",
)
# Variables that point a child at a service the host runs on its behalf. A
# child gets no Docker, session bus, agent or display endpoint.
SCRUBBED_PREFIXES = ("DOCKER_", "SSH_", "DBUS_", "GPG_", "KRB5", "XDG_RUNTIME_DIR")
SCRUBBED_NAMES = frozenset(
    {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "CONTAINER_HOST"}
)
LANDLOCK_FILE = Path(__file__).with_name("migration_upgrade_landlock.py")
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MS_REC = 0x4000
MS_PRIVATE = 1 << 18
RELAY_BLOCK = 1 << 16
SANDBOX_BODY = """
import json, os, stat, sys
spec = json.loads(sys.argv[1])
for path, identity in spec["identities"]:
    found = os.lstat(path)
    if not stat.S_ISDIR(found.st_mode) or f"{found.st_dev}:{found.st_ino}" != identity:
        sys.stderr.write(f"sandbox_entry_replaced: {path}\\n")
        raise SystemExit(125)
policy = spec["landlock"]
try:
    apply(policy["write_fds"], policy["read"], policy["ports"])
except OSError as error:
    sys.stderr.write(f"sandbox_landlock_refused: {error}\\n")
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
# The stub is the policy source followed by the launch steps, handed over as one
# argument: no file the invoking user can rewrite decides what the child may do.
SANDBOX_STUB = LANDLOCK_FILE.read_text(encoding="utf-8") + SANDBOX_BODY


def _stub_python() -> str:
    """Return the system interpreter that finishes setting up inside the sandbox."""
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        if Path(candidate).exists():
            return candidate
    return sys.executable


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


def _read_paths(environment: dict[str, str]) -> list[str]:
    """Return the host paths a child may read, minimal and absolute."""
    wanted = [*SYSTEM_READ_ONLY]
    wanted += [
        p
        for p in environment.get("UPGRADE_SANDBOX_READ_ONLY", "").split(os.pathsep)
        if p
    ]
    wanted += [p for p in environment.get("PATH", "").split(os.pathsep) if p]
    bound: list[str] = []
    for path in wanted:
        if not path.startswith("/") or any(
            path == known or path.startswith(known + "/") for known in bound
        ):
            continue
        bound.append(path)
    return bound


def _read_only_binds(paths: list[str]) -> list[str]:
    """Bind exactly the paths a child needs, each read-only and recursively."""
    arguments: list[str] = []
    for path in paths:
        arguments += ["--ro-bind-try", path, path]
    return arguments


def _connect_ports(environment: dict[str, str]) -> list[int] | None:
    """Return the TCP ports a child may reach, or None for unrestricted network."""
    named = environment.get("UPGRADE_SANDBOX_CONNECT_PORTS")
    if named is None:
        return None
    return [int(port) for port in named.split(",") if port]


def _scrub(environment: dict[str, str]) -> dict[str, str]:
    """Drop what points a child at a host service or at this launcher's own knobs."""
    return {
        key: value
        for key, value in environment.items()
        if key not in SCRUBBED_NAMES
        and not key.startswith(SCRUBBED_PREFIXES)
        and key not in {"UPGRADE_SANDBOX_STDIN", "UPGRADE_SANDBOX_CONNECT_PORTS"}
    }


def _load_landlock() -> ModuleType:
    """Load the policy module the stub embeds, so the launcher can probe it."""
    spec = importlib.util.spec_from_file_location("upgrade_landlock", LANDLOCK_FILE)
    if spec is None or spec.loader is None:
        message = "sandbox_landlock_module_missing"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(
    arguments: list[str], environment: dict[str, str]
) -> tuple[list[str], dict[str, str], bool]:
    """Return the launcher argv, the translated child environment and the stdin mode."""
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
    pipe_stdin = environment.get("UPGRADE_SANDBOX_STDIN") == "pipe"
    child_environment = {
        key: translate(value) for key, value in _scrub(environment).items()
    }
    read_paths = _read_paths(environment)
    ports = _connect_ports(environment)
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
    policy = {
        "write_fds": [int(run_fd)] if run_fd != "-" else [],
        "read": [*read_paths, "/proc"],
        "ports": ports,
    }
    spec = json.dumps({"identities": identities, "cwd": cwd, "landlock": policy})
    argv = [
        launcher,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp",  # noqa: S108 - a throwaway tmpfs, made read-only below
        *_read_only_binds(read_paths),
        "--symlink",
        "../run",
        "/var/run",
        "--dev",
        "/dev",
        "--tmpfs",
        SHM,
        *mount_arguments,
        "--remount-ro",
        SHM,
        "--proc",
        "/proc",
        "--remount-ro",
        "/tmp",  # noqa: S108 - see above
        "--remount-ro",
        "/",
        "--",
        _stub_python(),
        "-I",
        "-c",
        SANDBOX_STUB,
        spec,
        *translated,
    ]
    return argv, child_environment, pipe_stdin


def _private_mount_namespace() -> None:
    """Enter a mount namespace in which nothing propagates from the host."""
    libc = ctypes.CDLL(None, use_errno=True)
    uid, gid = os.getuid(), os.getgid()
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0:
        message = "sandbox_private_namespace_unavailable"
        raise OSError(ctypes.get_errno(), message)
    Path("/proc/self/setgroups").write_text("deny", encoding="ascii")
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1", encoding="ascii")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1", encoding="ascii")
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        message = "sandbox_private_mounts_unavailable"
        raise OSError(ctypes.get_errno(), message)


def _relay(source: int, destination: int | None) -> None:
    """Copy a pipe to a descriptor of this process until the pipe closes."""
    try:
        while block := os.read(source, RELAY_BLOCK):
            if destination is None:
                continue
            view = memoryview(block)
            while view:
                view = view[os.write(destination, view) :]
    except OSError:
        return
    finally:
        os.close(source)


def _feed(source: int, destination: int) -> None:
    """Hand this process's stdin to the child, then close the child's end."""
    try:
        while block := os.read(source, RELAY_BLOCK):
            view = memoryview(block)
            while view:
                view = view[os.write(destination, view) :]
    except OSError:
        return
    finally:
        os.close(destination)


def _run(
    argv: list[str], environment: dict[str, str], *, pipe_stdin: bool, keep: set[int]
) -> int:
    """Run the sandbox with pipes for every standard stream and relay them."""
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    in_read, in_write = os.pipe() if pipe_stdin else (-1, -1)
    child = subprocess.Popen(  # noqa: S603 - fixed sandbox launcher
        argv,
        env=environment,
        stdin=in_read if pipe_stdin else subprocess.DEVNULL,
        stdout=out_write,
        stderr=err_write,
        pass_fds=tuple(keep),
        close_fds=True,
    )
    for descriptor in (out_write, err_write, in_read):
        if descriptor >= 0:
            os.close(descriptor)

    def forward(number: int, _frame: FrameType | None) -> None:
        child.send_signal(number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, forward)
    workers = [
        threading.Thread(target=_relay, args=(out_read, 1)),
        threading.Thread(target=_relay, args=(err_read, 2)),
    ]
    for worker in workers:
        worker.start()
    if pipe_stdin:
        threading.Thread(target=_feed, args=(0, in_write), daemon=True).start()
    status = child.wait()
    for worker in workers:
        worker.join()
    return 128 - status if status < 0 else status


def main(arguments: list[str]) -> None:
    """Replace this process with the sandboxed child's exit status."""
    run_fd = arguments[1]
    try:
        _load_landlock().abi()
    except OSError as error:
        sys.stderr.write(f"sandbox_landlock_unavailable: {error}\n")
        raise SystemExit(125) from error
    argv, environment, pipe_stdin = build(arguments, dict(os.environ))
    keep = set() if run_fd == "-" else {int(run_fd)}
    for descriptor in keep:
        os.set_inheritable(descriptor, True)  # noqa: FBT003 - positional-only API
    _private_mount_namespace()
    raise SystemExit(_run(argv, environment, pipe_stdin=pipe_stdin, keep=keep))


if __name__ == "__main__":
    main(sys.argv[1:])
