"""Gunicorn hooks for the browser-test server only; never product config."""

from __future__ import annotations

from typing import Protocol

from ops.testing.browser_server_supervisor import die_with_parent


class _Arbiter(Protocol):
    pid: int


def post_fork(server: _Arbiter, _worker: object) -> None:
    """Have each worker die with its master, so none outlives the harness."""
    die_with_parent(server.pid)
