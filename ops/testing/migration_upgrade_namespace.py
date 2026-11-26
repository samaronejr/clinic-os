"""Start the rehearsal in a private mount namespace and relay interruptions."""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def _child_pid(receipt: int) -> int | None:
    # The launcher closes the receipt pipe before entering the payload.
    with os.fdopen(receipt, encoding="utf-8") as stream:
        data = stream.read()
    return int(json.loads(data)["child-pid"]) if data else None


def _launcher() -> str:
    launcher = shutil.which("bwrap")
    if launcher is None:
        message = "private_run_namespace_launcher_missing"
        raise RuntimeError(message)
    return launcher


def _pidfd(child: int | None) -> int | None:
    if child is None:
        return None
    try:
        return os.pidfd_open(child)
    except ProcessLookupError:
        return None


def run(entry: Path, arguments: list[str]) -> int:
    """Keep the launcher PID signalable while the inner shell owns its traps."""
    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    waiting: list[int] = []
    child_fd: int | None = None

    def relay(number: int, _frame: object) -> None:
        if child_fd is None:
            waiting.append(number)
            return
        try:
            signal.pidfd_send_signal(child_fd, number)
        except ProcessLookupError:
            # The sandboxed process has already exited.
            return

    for number in signals:
        signal.signal(number, relay)

    launcher = _launcher()
    host = Path("/dev/shm").stat()  # noqa: S108 - compare host and private mounts
    receipt, writer = os.pipe()
    try:
        with subprocess.Popen(  # noqa: S603 - fixed namespace launcher
            [
                launcher,
                "--die-with-parent",
                "--new-session",
                "--info-fd",
                str(writer),
                "--bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--tmpfs",
                "/dev/shm",  # noqa: S108 - mounted privately by this command
                "--setenv",
                "UPGRADE_HOST_SHM_DEVICE",
                str(host.st_dev),
                "--",
                str(entry.absolute()),
                *arguments,
            ],
            pass_fds=(writer,),
        ) as process:
            os.close(writer)
            writer = -1
            read_fd, receipt = receipt, -1
            child_fd = _pidfd(_child_pid(read_fd))
            if child_fd is not None:
                for queued in waiting:
                    relay(queued, None)
            status = process.wait()
    finally:
        for fd in (receipt, writer, child_fd):
            if fd is not None and fd >= 0:
                os.close(fd)
    return 128 - status if status < 0 else status


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2:]))
