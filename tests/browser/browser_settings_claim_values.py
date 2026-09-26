from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

MATERIALIZER_ID = "11111111-1111-4111-8111-111111111111"
DATABASE_ID = "22222222-2222-4222-8222-222222222222"
CA_EXPORT_ID = "33333333-3333-4333-8333-333333333333"
PROCESS_ID = "44444444-4444-4444-8444-444444444444"
PROCESS_PORT = 18443
PROJECT = "clinic_phase1a_browser_fixture"
DATABASE = f"{PROJECT}_clinic"
MATERIALIZER_NETWORK = "clinic_phase1a_materializer_fixture_network"
GUNICORN_CONFIG = (
    b'forwarded_allow_ips = ""\n'
    b"secure_scheme_headers = {}\n"
    b"# ADR-014: access logs carry method + status + duration only; the request\n"
    b"# line, query, remote address and headers are never logged (PHI boundary).\n"
    b'access_log_format = "%(m)s %(s)s %(D)s"\n'
)


def browser_environment(attempt_id: str, socket_path: Path) -> JsonObject:
    synthetic_tmp = Path(os.sep) / "tmp"
    literal = {
        "ALLOWED_HOSTS": "phase1a-browser.qa.clinic-os.test",
        "CLINIC_ATTEMPT_ID": attempt_id,
        "CLINIC_DATA_MODE": "synthetic",
        "CLINIC_LEDGER_RPC_SOCKET": str(socket_path),
        "CLINIC_PROCESS_CLAIM_ID": PROCESS_ID,
        "COMPOSE_PROJECT_NAME": PROJECT,
        "DJANGO_SETTINGS_MODULE": "config.settings.browser",
        "HOME": str(synthetic_tmp / "clinic-home"),
        "LANG": "C.UTF-8",
        "PYTHONTZPATH": "",
        "TMPDIR": str(synthetic_tmp),
    }
    return {
        "absent_keys": ["FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS"],
        "literal": [
            {"name": name, "value": value} for name, value in sorted(literal.items())
        ],
        "secret_keys": ["APP_DATABASE_URL", "SECRET_KEY"],
    }


def gunicorn_argv(launcher: Path, config: Path) -> list[str]:
    return [
        str(launcher),
        "-m",
        "gunicorn",
        "--config",
        str(config),
        "--preload",
        "--bind",
        f"127.0.0.1:{PROCESS_PORT}",
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


def claim_root(root: Path, claim_id: str) -> Path:
    path = root / claim_id
    path.mkdir(mode=0o700)
    return path


def argv_sha(argv: list[str]) -> str:
    return sha(b"".join(item.encode() + b"\0" for item in argv))


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_value(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def object_array(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    result: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


def strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def lstat_record(identity: os.stat_result, path: Path | None = None) -> JsonObject:
    return {
        "device": identity.st_dev,
        "gid": identity.st_gid,
        "inode": identity.st_ino,
        "link_count": identity.st_nlink,
        "mode": identity.st_mode & 0o7777,
        "symlink_target": str(path.readlink())
        if path is not None and path.is_symlink()
        else None,
        "uid": identity.st_uid,
    }
