"""Validate closed process reservations and immutable executable identity."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stack_service import (
    validate_environment_contract,
    validate_loopback_port,
)

DESIRED_KEYS: Final = frozenset(
    {
        "process_model",
        "launcher_path",
        "launcher_lstat",
        "interpreter_realpath",
        "interpreter_sha256",
        "argv",
        "argv_sha256",
        "module",
        "worker_count",
        "gunicorn_config_path",
        "gunicorn_config_lstat",
        "gunicorn_config_sha256",
        "uid",
        "gid",
        "environment_contract",
        "host_ports",
        "borrowed_file_refs",
    }
)
LSTAT_KEYS: Final = frozenset(
    {"device", "inode", "mode", "uid", "gid", "link_count", "symlink_target"}
)
BORROWED_FILE_KEYS: Final = frozenset(
    {
        "owner_claim_id",
        "relative_path",
        "mode",
        "uid",
        "gid",
        "device",
        "inode",
        "sha256",
        "access",
    }
)
UUID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
GUNICORN_CONFIG_SUFFIX: Final = ("ops", "container", "gunicorn_no_proxy.py")
GUNICORN_WORKER_COUNT: Final = 2


def _fail(message: str) -> Never:
    raise IsolationError(message)


def validate_process_desired(value: JsonObject) -> None:
    """Validate one process reservation and its live launcher dependencies."""
    if set(value) != DESIRED_KEYS:
        _fail("process desired has the wrong closed key set")
    launcher = _absolute_path(value["launcher_path"], "launcher path")
    _require_lstat(launcher, value["launcher_lstat"], "launcher")
    interpreter = _absolute_path(
        value["interpreter_realpath"],
        "interpreter realpath",
    )
    if launcher.resolve(strict=True) != interpreter:
        _fail("launcher does not resolve to the declared interpreter")
    _require_sha(interpreter, value["interpreter_sha256"], "interpreter")
    argv = _strings(value["argv"], "process argv")
    invalid_item = any(not item or "\0" in item for item in argv)
    if not argv or argv[0] != str(launcher) or invalid_item:
        _fail("process argv does not begin with the launcher")
    expected_argv_sha = hashlib.sha256(
        b"".join(item.encode() + b"\0" for item in argv)
    ).hexdigest()
    if value["argv_sha256"] != expected_argv_sha:
        _fail("process argv SHA-256 does not match")
    _executor_identity(value)
    validate_environment_contract(
        _object(value["environment_contract"], "environment contract")
    )
    ports = [
        validate_loopback_port(item, published=False)
        for item in _objects(value["host_ports"], "process host ports")
    ]
    if ports != sorted(set(ports)):
        _fail("process host ports are not sorted and unique")
    _borrowed_files(value["borrowed_file_refs"])
    _process_model(value, argv)


def reserved_process_observed() -> JsonObject:
    """Return the exact empty observed identity set for a reserved process."""
    return {"listener_socket_inode": None, "listeners": [], "members": []}


def validate_process_borrowed_dependencies(
    spec: JsonObject,
    claims: list[JsonObject],
) -> None:
    """Bind every borrowed file identity to one active filesystem dependency."""
    desired = _object(spec["desired"], "process desired")
    references = _objects(desired["borrowed_file_refs"], "borrowed file references")
    dependencies = _strings(spec["dependency_claim_ids"], "dependency claim IDs")
    for reference in references:
        owner_id = reference["owner_claim_id"]
        if owner_id not in dependencies:
            _fail("borrowed file owner is not a declared dependency")
        owners = [claim for claim in claims if claim.get("claim_id") == owner_id]
        if len(owners) != 1 or owners[0].get("kind") != "filesystem":
            _fail("borrowed file owner is not one filesystem claim")
        observed = _object(owners[0].get("observed"), "filesystem observation")
        files = _objects(observed.get("owned_files"), "filesystem owned files")
        expected = {key: reference[key] for key in BORROWED_FILE_KEYS - {"access"}}
        matches = [
            item
            for item in files
            if {key: item.get(key) for key in expected} == expected
        ]
        if len(matches) != 1:
            _fail("borrowed file identity is absent from its owner")


def _process_model(value: JsonObject, argv: list[str]) -> None:
    model = value["process_model"]
    if model == "single":
        _single_process_model(value)
        return
    if model != "gunicorn-prefork":
        _fail("process model is invalid")
    _gunicorn_process_model(value, argv)


def _single_process_model(value: JsonObject) -> None:
    if value["module"] is not None or value["worker_count"] is not None:
        _fail("single process has Gunicorn fields")
    config_fields = (
        value["gunicorn_config_path"],
        value["gunicorn_config_lstat"],
        value["gunicorn_config_sha256"],
    )
    if any(item is not None for item in config_fields):
        _fail("single process has Gunicorn config fields")


def _gunicorn_process_model(value: JsonObject, argv: list[str]) -> None:
    if value["module"] != "gunicorn" or value["worker_count"] != GUNICORN_WORKER_COUNT:
        _fail("Gunicorn module or worker count is invalid")
    config_fields = (
        value["gunicorn_config_path"],
        value["gunicorn_config_lstat"],
        value["gunicorn_config_sha256"],
    )
    if any(item is None for item in config_fields):
        _fail("Gunicorn config fields are incomplete")
    config = _absolute_path(config_fields[0], "Gunicorn config path")
    if tuple(config.parts[-3:]) != GUNICORN_CONFIG_SUFFIX:
        _fail("Gunicorn config path is not the no-proxy contract")
    _require_lstat(config, config_fields[1], "Gunicorn config")
    _require_sha(config.resolve(strict=True), config_fields[2], "Gunicorn config")
    if argv[1:3] != ["-m", "gunicorn"]:
        _fail("Gunicorn argv does not use the declared module")
    if argv.count("--config") != 1:
        _fail("Gunicorn argv has the wrong config option")
    config_index = argv.index("--config")
    if config_index + 1 >= len(argv) or argv[config_index + 1] != str(config):
        _fail("Gunicorn argv config path disagrees")
    if argv.count("--preload") != 1 or argv.count("--workers=2") != 1:
        _fail("Gunicorn argv lacks fixed preload worker identity")


def _borrowed_files(value: JsonValue) -> None:
    references = _objects(value, "borrowed file references")
    keys: list[tuple[str, str]] = []
    for item in references:
        if set(item) != BORROWED_FILE_KEYS or item.get("access") != "read-only":
            _fail("borrowed file reference has the wrong closed contract")
        owner = _text(item["owner_claim_id"], "borrowed file owner")
        if UUID_PATTERN.fullmatch(owner) is None:
            _fail("borrowed file owner ID is invalid")
        relative = _text(item["relative_path"], "borrowed file path")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative:
            _fail("borrowed file path escapes its owner root")
        for field in ("mode", "uid", "gid", "device", "inode"):
            _nonnegative_integer(item[field], f"borrowed file {field}")
        digest = _text(item["sha256"], "borrowed file SHA-256")
        if SHA256_PATTERN.fullmatch(digest) is None:
            _fail("borrowed file SHA-256 is invalid")
        keys.append((owner, relative))
    if keys != sorted(set(keys)):
        _fail("borrowed file references are not sorted and unique")


def _require_lstat(path: Path, expected: JsonValue, context: str) -> None:
    identity = _object(expected, f"{context} lstat")
    if set(identity) != LSTAT_KEYS:
        _fail(f"{context} lstat has the wrong closed key set")
    observed = os.lstat(path)
    if not (stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode)):
        _fail(f"{context} is not a regular file or symlink")
    target = str(path.readlink()) if stat.S_ISLNK(observed.st_mode) else None
    actual: JsonObject = {
        "device": observed.st_dev,
        "gid": observed.st_gid,
        "inode": observed.st_ino,
        "link_count": observed.st_nlink,
        "mode": stat.S_IMODE(observed.st_mode),
        "symlink_target": target,
        "uid": observed.st_uid,
    }
    if identity != actual:
        _fail(f"{context} lstat identity drifted")


def _require_sha(path: Path, value: JsonValue, context: str) -> None:
    digest = _text(value, f"{context} SHA-256")
    if SHA256_PATTERN.fullmatch(digest) is None or digest != _file_sha256(path):
        _fail(f"{context} SHA-256 drifted")


def _file_sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail("resolved executable identity is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _executor_identity(value: JsonObject) -> None:
    if value["uid"] != os.geteuid() or value["gid"] != os.getegid():
        _fail("process identity is not the executor UID/GID")


def _absolute_path(value: JsonValue, context: str) -> Path:
    path = Path(_text(value, context))
    if not path.is_absolute():
        _fail(f"{context} is not absolute")
    return path


def _nonnegative_integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} is invalid")
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
