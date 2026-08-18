"""Hold a review-stage payload behind its parent-owned release barrier."""

from __future__ import annotations

import argparse
import ctypes
import os
import signal


def main() -> int:
    """Signal readiness, wait for release, and replace this process."""
    arguments = _parser().parse_args()
    if os.getppid() != arguments.parent_pid:
        return 125
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL) != 0 or os.getppid() != arguments.parent_pid:
        return 125
    os.write(arguments.ready_fd, b"R")
    if os.read(arguments.release_fd, 1) != b"G":
        return 125
    os.close(arguments.ready_fd)
    os.close(arguments.release_fd)
    payload = tuple(os.fsencode(item) for item in arguments.payload)
    environment = tuple(key + b"=" + value for key, value in os.environb.items())
    argv = (ctypes.c_char_p * (len(payload) + 1))(*payload, None)
    envp = (ctypes.c_char_p * (len(environment) + 1))(*environment, None)
    _ = libc.execve(payload[0], argv, envp)
    return 126


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--ready-fd", type=int, required=True)
    parser.add_argument("--release-fd", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("payload", nargs=argparse.REMAINDER)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
