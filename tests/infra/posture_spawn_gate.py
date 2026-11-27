"""Inert pre-exec barrier: escrow our own pidfd before starting any workload."""

from __future__ import annotations

import os
import signal
import socket
import sys
from pathlib import Path


def main() -> int:
    mode = os.environ.pop("CLINIC_TEST_GATE_KIND")
    if mode not in {"make", "worker"}:
        return 125
    with socket.socket(fileno=int(os.environ.pop("CLINIC_TEST_GATE_FD"))) as channel:
        descriptor = os.pidfd_open(os.getpid())
        try:
            if socket.send_fds(channel, [b"P"], [descriptor]) != 1:
                return 125
        finally:
            os.close(descriptor)
        if channel.recv(1) != b"G":
            return 125
    if (
        not Path(sys.executable).is_absolute()
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
    ):
        return 125
    # The admitted launcher remains the handle-bound leader until the fixed
    # workload has exited. Descendants inherit its private signal domain.
    if mode == "make":
        pid = os.posix_spawn(
            "/usr/bin/make", ("/usr/bin/make", "db-posture", "DOCKER=true"), os.environ
        )
    else:
        pid = os.posix_spawn(
            sys.executable,
            (sys.executable, "-m", "infra.posture_trace_worker"),
            os.environ,
        )
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    inherited = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        raise SystemExit(main())
    finally:
        signal.signal(signal.SIGCHLD, inherited)
