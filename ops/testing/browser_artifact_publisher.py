"""Bounded framed export of one suite's allowlisted browser artifact set.

Artifacts leave the runner only as canonical length-bounded frames: one
manifest naming every path, size, and digest, then fixed-size chunks. No frame
may name a path outside the suite's own `browser/<suite_id>/` prefix, so a
suite can never publish another suite's evidence or an unbounded capture.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.browser_runner_contract import selected_suites

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
CHUNK_BYTES: Final = 64 * 1024
ALLOWED_SUFFIXES: Final = frozenset({".json", ".png"})
ACKNOWLEDGEMENT_KEYS: Final = frozenset(
    {"accepted", "manifest_sha256", "schema_version", "suite_id"}
)


class ArtifactPublicationError(RuntimeError):
    """Reject an unbounded, misnamed, or unacknowledged artifact export."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying publication failure."""
        super().__init__(f"artifact publication rejected: {reason}")


def _fail(reason: str) -> Never:
    raise ArtifactPublicationError(reason)


def artifact_prefix(suite_id: str) -> str:
    """Return the only path prefix this suite may ever publish under."""
    selected_suites([suite_id], [suite_id])
    return f"browser/{suite_id}/"


def _validate(suite_id: str, path: str, content: bytes) -> None:
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != path
        or not path.startswith(artifact_prefix(suite_id))
    ):
        _fail(f"{path} is outside the {suite_id} artifact prefix")
    if pure.suffix not in ALLOWED_SUFFIXES:
        _fail(f"{path} has a forbidden suffix")
    if not content or len(content) > MAX_ARTIFACT_BYTES:
        _fail(f"{path} is empty or exceeds the bounded artifact size")


def frame_suite_artifacts(
    suite_id: str,
    artifacts: dict[str, bytes],
) -> tuple[bytes, ...]:
    """Return the canonical manifest frame followed by bounded chunk frames."""
    if not artifacts or list(artifacts) != sorted(artifacts):
        _fail("artifact set is empty or unsorted")
    if f"{artifact_prefix(suite_id)}summary.json" not in artifacts:
        _fail(f"{suite_id} published no summary")
    entries: list[JsonValue] = []
    chunks: list[bytes] = []
    for path, content in artifacts.items():
        _validate(suite_id, path, content)
        entries.append(
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
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
        {"entries": entries, "schema_version": 1, "suite_id": suite_id}
    )
    return (manifest, *chunks)


def manifest_digest(frames: tuple[bytes, ...]) -> str:
    """Return the digest of the manifest frame every chunk is bound to."""
    if not frames:
        _fail("no manifest frame was produced")
    return hashlib.sha256(frames[0]).hexdigest()


def publication_acknowledgement(suite_id: str, digest: str) -> bytes:
    """Build the final host acknowledgement for one published manifest."""
    return _canonical(
        {
            "accepted": True,
            "manifest_sha256": digest,
            "schema_version": 1,
            "suite_id": suite_id,
        }
    )


def require_acknowledged(raw: bytes, suite_id: str, digest: str) -> None:
    """Reject every acknowledgement not bound to this suite and manifest."""
    if raw != publication_acknowledgement(suite_id, digest):
        _fail("publication was never acknowledged for this manifest")


def _canonical(value: JsonObject) -> bytes:
    return rfc8785.dumps(value) + b"\n"
