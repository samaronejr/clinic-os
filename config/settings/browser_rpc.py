from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Never

from django.core.exceptions import ImproperlyConfigured
from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes

if TYPE_CHECKING:
    from pathlib import Path

RESPONSE_KEYS: Final = frozenset(
    {
        "attempt_id",
        "claim_id",
        "claim_status",
        "ledger_sha256",
        "observation_sha256",
        "schema_version",
        "sequence",
        "verified_at_utc",
    }
)
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
UUID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
FRESHNESS: Final = timedelta(seconds=60)
MAX_PACKET_BYTES: Final = 4096
RPC_TIMEOUT_SECONDS: Final = 5.0
TIMESTAMP_LENGTH: Final = 27
PRIVATE_SOCKET_MODE: Final = 0o600
PEER_CREDENTIAL_FORMAT: Final = "3i"


def receive_reserved_attestation(
    socket_path: Path,
    *,
    attempt_id: str,
    claim_id: str,
    sequence: int = 1,
) -> JsonObject:
    try:
        expected_socket = _socket_identity(socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
            connection.settimeout(RPC_TIMEOUT_SECONDS)
            connection.connect(str(socket_path))
            if _socket_identity(socket_path) != expected_socket:
                _fail()
            _validate_peer(connection)
            raw, ancillary, flags, _ = connection.recvmsg(MAX_PACKET_BYTES)
        if (
            not raw
            or ancillary
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or len(raw) == MAX_PACKET_BYTES
        ):
            _fail()
        value: JsonValue = json.loads(raw)
        if not isinstance(value, dict):
            _fail()
        response: JsonObject = {str(key): item for key, item in value.items()}
        if canonical_bytes(response) != raw:
            _fail()
        return validate_attestation_response(
            response,
            attempt_id=attempt_id,
            claim_id=claim_id,
            status="reserved",
            sequence=sequence,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        _fail()


def validate_attestation_response(
    response: JsonObject,
    *,
    attempt_id: str,
    claim_id: str,
    status: str,
    sequence: int,
) -> JsonObject:
    if set(response) != RESPONSE_KEYS:
        _fail()
    if (
        response.get("schema_version") != 1
        or isinstance(response.get("schema_version"), bool)
        or response.get("sequence") != sequence
        or isinstance(response.get("sequence"), bool)
        or response.get("attempt_id") != attempt_id
        or response.get("claim_id") != claim_id
        or response.get("claim_status") != status
        or status not in {"reserved", "active"}
    ):
        _fail()
    try:
        _canonical_uuid(attempt_id)
        _canonical_uuid(claim_id)
        ledger_sha = _text(response.get("ledger_sha256"))
        observation_sha = _text(response.get("observation_sha256"))
        verified_at = _timestamp(_text(response.get("verified_at_utc")))
    except (TypeError, ValueError):
        _fail()
    if (
        SHA256_PATTERN.fullmatch(ledger_sha) is None
        or SHA256_PATTERN.fullmatch(observation_sha) is None
        or not datetime.now(UTC) - FRESHNESS <= verified_at <= datetime.now(UTC)
    ):
        _fail()
    return response


def _timestamp(value: str) -> datetime:
    if len(value) != TIMESTAMP_LENGTH or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise ValueError
    return parsed


def _canonical_uuid(value: str) -> None:
    if UUID_PATTERN.fullmatch(value) is None:
        raise ValueError


def _socket_identity(path: Path) -> tuple[int, int, int, int, int]:
    identity = path.lstat()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISSOCK(identity.st_mode)
        or stat.S_IMODE(identity.st_mode) != PRIVATE_SOCKET_MODE
        or identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
    ):
        raise ValueError
    return (
        identity.st_dev,
        identity.st_ino,
        stat.S_IMODE(identity.st_mode),
        identity.st_uid,
        identity.st_gid,
    )


def _validate_peer(connection: socket.socket) -> None:
    size = struct.calcsize(PEER_CREDENTIAL_FORMAT)
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    pid, uid, gid = struct.unpack(PEER_CREDENTIAL_FORMAT, credentials)
    if pid < 1 or uid != os.geteuid() or gid != os.getegid():
        raise ValueError


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _fail() -> Never:
    message = "browser attestation violates the closed response contract"
    raise ImproperlyConfigured(message)
