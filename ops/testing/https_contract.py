"""Exact Gunicorn TLS override used only by the isolated HTTPS harness."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

TMPFS_TARGET: Final = PurePosixPath("/", "tmp").as_posix()

DEFAULT_COMMAND: Final = (
    "gunicorn",
    "--config=/app/ops/container/gunicorn_no_proxy.py",
    "--bind=0.0.0.0:8000",
    "--workers=2",
    "--threads=4",
    "--timeout=30",
    "--graceful-timeout=30",
    "--keep-alive=5",
    "--max-requests=1000",
    "--max-requests-jitter=100",
    "--access-logfile=-",
    "--error-logfile=-",
    "config.wsgi:application",
)
TLS_COMMAND: Final = (
    "gunicorn",
    "--config=/app/ops/container/gunicorn_no_proxy.py",
    "--bind=0.0.0.0:8443",
    "--certfile=/run/clinic-test-tls/tls/tls.crt",
    "--keyfile=/run/clinic-test-tls/tls/tls.key",
    "--workers=2",
    "--threads=4",
    "--timeout=30",
    "--graceful-timeout=30",
    "--keep-alive=5",
    "--max-requests=1000",
    "--max-requests-jitter=100",
    "--access-logfile=-",
    "--error-logfile=-",
    "config.wsgi:application",
)


def application_filesystem_contract() -> JsonObject:
    """Keep the application root immutable with one bounded private tmpfs."""
    return {
        "ipc_mode": "none",
        "root_read_only": True,
        "shm_size_bytes": 0,
        "tmpfs_mounts": [
            {
                "gid": 10001,
                "mode": 0o700,
                "nodev": True,
                "noexec": True,
                "nosuid": True,
                "size_bytes": 67_108_864,
                "target": TMPFS_TARGET,
                "uid": 10001,
            }
        ],
        "writable_paths": [TMPFS_TARGET],
    }
