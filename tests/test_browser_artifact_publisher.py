from __future__ import annotations

import base64
import hashlib
import json
from typing import Final

import pytest
from ops.testing.browser_artifact_publisher import (
    CHUNK_BYTES,
    MAX_ARTIFACT_BYTES,
    ArtifactPublicationError,
    artifact_prefix,
    frame_suite_artifacts,
    manifest_digest,
    publication_acknowledgement,
    require_acknowledged,
)

SUITE: Final = "availability"
PREFIX: Final = "browser/availability"
SUMMARY: Final = f"{PREFIX}/summary.json"
SHOT: Final = f"{PREFIX}/blank.png"


def _artifacts(**extra: bytes) -> dict[str, bytes]:
    return dict(sorted({SUMMARY: b'{"a":1}\n', **extra}.items()))


def _decoded(frames: tuple[bytes, ...]) -> dict[str, bytes]:
    rebuilt: dict[str, bytes] = {}
    for raw in frames[1:]:
        chunk = json.loads(raw)
        rebuilt[chunk["path"]] = rebuilt.get(chunk["path"], b"") + base64.b64decode(
            chunk["data_base64"]
        )
    return rebuilt


def test_prefix_is_derived_only_from_a_known_suite() -> None:
    assert artifact_prefix(SUITE) == f"{PREFIX}/"
    with pytest.raises(RuntimeError):
        artifact_prefix("not-a-suite")


def test_frames_reconstruct_every_artifact_byte_for_byte() -> None:
    artifacts = _artifacts(**{SHOT: b"\x89PNG\r\n" + b"p" * (CHUNK_BYTES + 7)})

    frames = frame_suite_artifacts(SUITE, artifacts)

    manifest = json.loads(frames[0])
    assert manifest["suite_id"] == SUITE
    assert [entry["path"] for entry in manifest["entries"]] == sorted(artifacts)
    for entry in manifest["entries"]:
        content = artifacts[entry["path"]]
        assert entry["size_bytes"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert _decoded(frames) == artifacts


def test_every_chunk_frame_carries_its_own_bounded_digest() -> None:
    frames = frame_suite_artifacts(
        SUITE, _artifacts(**{SHOT: b"z" * (2 * CHUNK_BYTES)})
    )

    for raw in frames[1:]:
        chunk = json.loads(raw)
        payload = base64.b64decode(chunk["data_base64"])
        assert len(payload) <= CHUNK_BYTES
        assert hashlib.sha256(payload).hexdigest() == chunk["sha256"]


@pytest.mark.parametrize(
    ("artifacts", "match"),
    [
        ({}, "empty or unsorted"),
        ({f"{PREFIX}/a.png": b"x"}, "published no summary"),
        ({SUMMARY: b"{}", "browser/patient/x.png": b"y"}, "artifact prefix"),
        ({SUMMARY: b"{}", "/etc/passwd": b"y"}, "artifact prefix"),
        ({SUMMARY: b"{}", f"{PREFIX}/../x.png": b"y"}, "artifact prefix"),
        ({SUMMARY: b"{}", f"{PREFIX}/x.har": b"y"}, "forbidden suffix"),
        ({SUMMARY: b""}, "bounded artifact size"),
    ],
)
def test_every_unbounded_or_foreign_artifact_is_refused(
    artifacts: dict[str, bytes],
    match: str,
) -> None:
    with pytest.raises(ArtifactPublicationError, match=match):
        frame_suite_artifacts(SUITE, dict(sorted(artifacts.items())))


def test_an_oversized_artifact_is_refused() -> None:
    with pytest.raises(ArtifactPublicationError, match="bounded artifact size"):
        frame_suite_artifacts(
            SUITE, _artifacts(**{SHOT: b"q" * (MAX_ARTIFACT_BYTES + 1)})
        )


def test_an_unsorted_artifact_set_is_refused() -> None:
    unsorted = {SUMMARY: b"{}", SHOT: b"\x89PNG"}
    assert list(unsorted) != sorted(unsorted)

    with pytest.raises(ArtifactPublicationError, match="empty or unsorted"):
        frame_suite_artifacts(SUITE, unsorted)


def test_acknowledgement_is_bound_to_the_exact_manifest() -> None:
    frames = frame_suite_artifacts(SUITE, _artifacts())
    digest = manifest_digest(frames)

    require_acknowledged(publication_acknowledgement(SUITE, digest), SUITE, digest)

    for raw, suite_id, wrong in (
        (b"", SUITE, digest),
        (publication_acknowledgement("patient", digest), SUITE, digest),
        (publication_acknowledgement(SUITE, "0" * 64), SUITE, digest),
        (publication_acknowledgement(SUITE, digest), SUITE, "0" * 64),
    ):
        with pytest.raises(ArtifactPublicationError, match="never acknowledged"):
            require_acknowledged(raw, suite_id, wrong)


def test_manifest_digest_requires_a_manifest_frame() -> None:
    with pytest.raises(ArtifactPublicationError, match="no manifest frame"):
        manifest_digest(())
