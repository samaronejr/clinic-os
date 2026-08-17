from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_process_observation import validate_process_observation

from .browser_authority_root import integer, object_array, object_value, text

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

GUNICORN_CONFIG: Final = b'forwarded_allow_ips = ""\nsecure_scheme_headers = {}\n'
DESIRED_KEYS: Final = frozenset(
    {
        "argv",
        "argv_sha256",
        "borrowed_file_refs",
        "environment_contract",
        "gid",
        "gunicorn_config_lstat",
        "gunicorn_config_path",
        "gunicorn_config_sha256",
        "host_ports",
        "interpreter_realpath",
        "interpreter_sha256",
        "launcher_lstat",
        "launcher_path",
        "module",
        "process_model",
        "uid",
        "worker_count",
    }
)
GUNICORN_WORKER_COUNT: Final = 2


def validate_browser_process(
    claim: JsonObject,
    expected: JsonObject,
    phase: str,
    ca_file: JsonObject,
) -> None:
    desired = object_value(claim.get("desired"))
    if set(desired) != DESIRED_KEYS:
        raise ValueError
    launcher = Path(text(desired.get("launcher_path")))
    interpreter = Path(text(desired.get("interpreter_realpath")))
    config = Path(text(desired.get("gunicorn_config_path")))
    process_port = integer(expected.get("process_port"))
    if (
        desired.get("process_model") != "gunicorn-prefork"
        or desired.get("module") != "gunicorn"
        or desired.get("worker_count") != GUNICORN_WORKER_COUNT
        or desired.get("uid") != os.geteuid()
        or desired.get("gid") != os.getegid()
        or tuple(launcher.parts[-3:]) != (".venv", "bin", "python")
        or tuple(config.parts[-3:]) != ("ops", "container", "gunicorn_no_proxy.py")
        or launcher.resolve(strict=True) != interpreter
        or desired.get("environment_contract") != expected.get("environment_contract")
        or desired.get("host_ports")
        != [{"host": "127.0.0.1", "port": process_port, "transport": "tcp"}]
    ):
        raise ValueError
    _require_lstat(launcher, object_value(desired.get("launcher_lstat")))
    _require_lstat(config, object_value(desired.get("gunicorn_config_lstat")))
    _require_hash(interpreter, text(desired.get("interpreter_sha256")))
    _require_hash(config, text(desired.get("gunicorn_config_sha256")))
    if config.read_bytes() != GUNICORN_CONFIG:
        raise ValueError
    expected_argv = _expected_argv(launcher, config, process_port)
    argv = _strings(desired.get("argv"))
    if argv != expected_argv or desired.get("argv_sha256") != _argv_hash(argv):
        raise ValueError
    references = object_array(desired.get("borrowed_file_refs"))
    expected_reference = {"access": "read-only", **ca_file}
    if references != [expected_reference]:
        raise ValueError
    observed = object_value(claim.get("observed"))
    if phase == "reserved":
        if observed != {"listener_socket_inode": None, "listeners": [], "members": []}:
            raise ValueError
    else:
        validate_process_observation(observed, claim)


def _expected_argv(launcher: Path, config: Path, port: int) -> list[str]:
    return [
        str(launcher),
        "-m",
        "gunicorn",
        "--config",
        str(config),
        "--preload",
        "--bind",
        f"127.0.0.1:{port}",
        "--workers=2",
        "--threads",
        "1",
        "--timeout",
        "30",
        "--graceful-timeout",
        "30",
        "--keep-alive",
        "5",
        "--max-requests",
        "0",
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
        "config.wsgi:application",
    ]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError
    return [item for item in value if isinstance(item, str)]


def _argv_hash(argv: list[str]) -> str:
    return hashlib.sha256(b"".join(item.encode() + b"\0" for item in argv)).hexdigest()


def _require_hash(path: Path, expected: str) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError


def _require_lstat(path: Path, expected: JsonObject) -> None:
    identity = path.lstat()
    actual: JsonObject = {
        "device": identity.st_dev,
        "gid": identity.st_gid,
        "inode": identity.st_ino,
        "link_count": identity.st_nlink,
        "mode": identity.st_mode & 0o7777,
        "symlink_target": str(path.readlink()) if path.is_symlink() else None,
        "uid": identity.st_uid,
    }
    if expected != actual:
        raise ValueError
