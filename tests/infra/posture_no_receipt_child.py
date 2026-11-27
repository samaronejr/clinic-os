"""Inert synchronization endpoint for the missing-receipt regression."""

from __future__ import annotations

import os
import signal
import socket
import sys

if __name__ == "__main__":
    with socket.socket(fileno=int(os.environ["CLINIC_TEST_GATE_FD"])) as endpoint:
        if sys.argv[1:] == ["malformed"]:
            endpoint.sendall(b"X")
    os.write(1, b"R")
    if sys.argv[1:] == ["silent-live"]:
        signal.pause()
