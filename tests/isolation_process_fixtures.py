from __future__ import annotations

import hashlib
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from ops.testing.isolation_common import JsonObject, JsonValue, raw_sha256

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


def process_spec(tmp_path: Path, claim_id: str) -> JsonObject:
    launcher = tmp_path / "process-launcher"
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    argv = [str(launcher), "--serve"]
    return process_spec_for_argv(claim_id, launcher, argv)


def process_spec_for_argv(
    claim_id: str,
    launcher: Path,
    argv: Sequence[str],
    *,
    host_ports: list[JsonObject] | None = None,
) -> JsonObject:
    identity = launcher.lstat()
    desired: JsonObject = {
        "argv": cast("JsonValue", list(argv)),
        "argv_sha256": hashlib.sha256(
            b"".join(item.encode() + b"\0" for item in argv)
        ).hexdigest(),
        "borrowed_file_refs": [],
        "environment_contract": {
            "absent_keys": ["FORWARDED_ALLOW_IPS"],
            "literal": [{"name": "CLINIC_DATA_MODE", "value": "synthetic"}],
            "secret_keys": [],
        },
        "gid": os.getegid(),
        "gunicorn_config_lstat": None,
        "gunicorn_config_path": None,
        "gunicorn_config_sha256": None,
        "host_ports": cast(
            "JsonValue",
            host_ports
            if host_ports is not None
            else [{"host": "127.0.0.1", "port": 18080, "transport": "tcp"}],
        ),
        "interpreter_realpath": str(launcher.resolve(strict=True)),
        "interpreter_sha256": raw_sha256(launcher.read_bytes()),
        "launcher_lstat": {
            "device": identity.st_dev,
            "gid": identity.st_gid,
            "inode": identity.st_ino,
            "link_count": identity.st_nlink,
            "mode": identity.st_mode & 0o777,
            "symlink_target": (
                str(launcher.readlink()) if launcher.is_symlink() else None
            ),
            "uid": identity.st_uid,
        },
        "launcher_path": str(launcher),
        "module": None,
        "process_model": "single",
        "uid": os.geteuid(),
        "worker_count": None,
    }
    return {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": desired,
        "kind": "process",
        "purpose": "host-http",
    }


def blocking_python_argv() -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)",
    )


@contextmanager
def running_process(argv: Sequence[str]) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(  # noqa: S603
        tuple(argv),
        executable=sys.executable,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if process.stdout is None or not select.select([process.stdout], [], [], 5)[0]:
            message = "process readiness timed out"
            raise RuntimeError(message)
        if process.stdout.readline() != b"ready\n":
            message = "process readiness failed"
            raise RuntimeError(message)
        yield process
    finally:
        if process.poll() is None:
            if process.stdin is not None:
                process.stdin.write(b"1")
                process.stdin.flush()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
