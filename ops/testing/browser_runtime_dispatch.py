"""Canonical in-process runtime-suite dispatch and bounded artifact framing."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Never

import rfc8785
from typing_extensions import TypeIs

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_common import JsonObject, JsonValue

MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
CHUNK_BYTES: Final = 64 * 1024
FRAME_KEYS: Final = frozenset(
    {"claim_id", "schema_version", "suite_id", "vector", "vector_sha256"}
)
type RuntimeSuite = Callable[[], dict[str, bytes]]


def build_runtime_dispatch_frame(claim_id: str, vector: list[str]) -> bytes:
    """Build one canonical claim-bound runtime-HTTPS suite frame."""
    vector_hash = hashlib.sha256(rfc8785.dumps(vector)).hexdigest()
    value: JsonObject = {
        "claim_id": claim_id,
        "schema_version": 1,
        "suite_id": "runtime-https",
        "vector": _json_strings(vector),
        "vector_sha256": vector_hash,
    }
    return rfc8785.dumps(value) + b"\n"


def dispatch_runtime_frame(
    raw: bytes,
    claim_id: str,
    registry: dict[str, RuntimeSuite],
) -> tuple[bytes, tuple[bytes, ...]]:
    """Validate, dispatch one fixture callable, and frame its bounded artifacts."""
    frame = _canonical_object(raw)
    vector = _strings(frame.get("vector"))
    suite_id = frame.get("suite_id")
    vector_hash = hashlib.sha256(rfc8785.dumps(vector)).hexdigest()
    if (
        set(frame) != FRAME_KEYS
        or frame.get("schema_version") != 1
        or frame.get("claim_id") != claim_id
        or suite_id != "runtime-https"
        or frame.get("vector_sha256") != vector_hash
        or set(registry) != {"runtime-https"}
    ):
        _fail()
    suite = registry["runtime-https"]
    artifacts = suite()
    entries: list[JsonValue] = []
    chunks: list[bytes] = []
    if list(artifacts) != sorted(artifacts):
        _fail()
    for path, content in artifacts.items():
        _validate_artifact(path, content)
        digest = hashlib.sha256(content).hexdigest()
        entries.append({"path": path, "sha256": digest, "size_bytes": len(content)})
        for index, offset in enumerate(range(0, len(content), CHUNK_BYTES)):
            chunk = content[offset : offset + CHUNK_BYTES]
            chunks.append(
                _canonical(
                    {
                        "data_base64": base64.b64encode(chunk).decode("ascii"),
                        "index": index,
                        "path": path,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(chunk).hexdigest(),
                    }
                )
            )
    manifest = _canonical(
        {
            "entries": entries,
            "schema_version": 1,
            "suite_id": suite_id,
            "vector_sha256": vector_hash,
        }
    )
    acknowledgement = _canonical(
        {
            "accepted": True,
            "claim_id": claim_id,
            "schema_version": 1,
            "suite_id": suite_id,
            "vector_sha256": vector_hash,
        }
    )
    return acknowledgement, (manifest, *chunks)


def _validate_artifact(path: str, content: bytes) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != path
        or not content
        or len(content) > MAX_ARTIFACT_BYTES
    ):
        _fail()


def _canonical(value: JsonObject) -> bytes:
    return rfc8785.dumps(value) + b"\n"


def _canonical_object(raw: bytes) -> JsonObject:
    value: object = json.loads(raw)
    if not isinstance(value, dict) or raw != rfc8785.dumps(value) + b"\n":
        _fail()
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _is_json_value(item):
            _fail()
        result[key] = item
    return result


def _is_json_value(value: object) -> TypeIs[JsonValue]:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        _fail()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail()
        result.append(item)
    return result


def _json_strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def _fail() -> Never:
    raise _RuntimeDispatchError


class _RuntimeDispatchError(RuntimeError):
    pass
