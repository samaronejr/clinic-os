from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, cast

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.shared_evidence_baseline import capture_manifest, verify_manifest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ATTEMPT_ID: Final = "12345678-1234-4123-8123-123456789abc"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    evidence_root = tmp_path / "evidence"
    nested = evidence_root / "history" / "nested"
    nested.mkdir(parents=True)
    record = nested / "record.json"
    record.write_bytes(b'{"stable":true}\n')
    return evidence_root, record


def _change_content(record: Path, _evidence_root: Path) -> None:
    record.write_bytes(b'{"stable":false}\n')


def _change_mode(record: Path, _evidence_root: Path) -> None:
    record.chmod(0o600)


def _replace_inode(record: Path, _evidence_root: Path) -> None:
    replacement = record.with_suffix(".replacement")
    replacement.write_bytes(b'{"stable":true}\n')
    replacement.replace(record)


@pytest.mark.parametrize(
    "mutation",
    [_change_content, _change_mode, _replace_inode],
    ids=["content", "mode", "inode"],
)
def test_manifest_rejects_regular_entry_drift(
    tmp_path: Path,
    mutation: Callable[[Path, Path], None],
) -> None:
    # Given: immutable bytes and lstat metadata for one protected regular file.
    evidence_root, record = _tree(tmp_path)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: one protected regular-file identity dimension drifts.
    mutation(record, evidence_root)

    # Then: verification rejects before treating shared history as unchanged.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def test_manifest_rejects_regular_link_count_drift(tmp_path: Path) -> None:
    # Given: one protected regular file with a single directory entry.
    evidence_root, record = _tree(tmp_path)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: a task-owned excluded path hard-links the protected inode.
    excluded = evidence_root / "clinic-os-phase1a-runtime" / ATTEMPT_ID
    excluded.mkdir(parents=True)
    os.link(record, excluded / "borrowed.json")

    # Then: the unrelated file's changed link count still rejects.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def test_manifest_rejects_symlink_target_drift(tmp_path: Path) -> None:
    # Given: one protected symlink recorded without following its target.
    evidence_root, _ = _tree(tmp_path)
    link = evidence_root / "history-link"
    link.symlink_to("history")
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: the symlink name is rebound to a different textual target.
    link.unlink()
    link.symlink_to("other-history")

    # Then: link-target and inode drift are rejected.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def test_manifest_never_follows_a_symlinked_directory(tmp_path: Path) -> None:
    # Given: an unrelated evidence symlink points to a populated external directory.
    evidence_root, _ = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "external.json"
    external.write_bytes(b'{"version":1}\n')
    link = evidence_root / "external-link"
    link.symlink_to(outside, target_is_directory=True)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: bytes beneath the external target change without changing the symlink.
    external.write_bytes(b'{"version":2}\n')

    # Then: verification compares only the recorded link identity and target text.
    verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)
    paths = _paths(expected)
    assert "external-link" in paths
    assert not any(path.startswith("external-link/") for path in paths)


@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_manifest_rejects_nested_entry_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    # Given: one protected nested directory with a stable starting entry set.
    evidence_root, record = _tree(tmp_path)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: one nested entry is added or removed.
    if mutation == "add":
        record.with_name("added.json").write_bytes(b"{}\n")
    else:
        record.unlink()

    # Then: the recursive Merkle inventory rejects the changed entry set.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def test_manifest_rejects_nested_directory_mode_drift(tmp_path: Path) -> None:
    # Given: the protected nested directory's mode is part of the baseline.
    evidence_root, record = _tree(tmp_path)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: only the nested directory mode changes.
    record.parent.chmod(0o700)

    # Then: metadata-only directory drift rejects.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_manifest_rejects_recorded_owner_metadata_drift(
    tmp_path: Path,
    field: str,
) -> None:
    # Given: a canonical manifest binds the protected file's owner metadata.
    evidence_root, record = _tree(tmp_path)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)
    entry = _entry(expected, record.relative_to(evidence_root).as_posix())

    # When: the recorded owner identity is changed independently of the tree.
    value = entry[field]
    assert isinstance(value, int)
    entry[field] = value + 1

    # Then: runtime verification rejects the owner mismatch.
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def test_capture_rejects_a_named_file_replaced_during_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a protected path is swapped after its inode is opened for hashing.
    evidence_root, record = _tree(tmp_path)
    identity = record.stat(follow_symlinks=False)
    original_read = os.read
    swapped = False

    def read_and_swap(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        opened = os.fstat(descriptor)
        if (
            not swapped
            and opened.st_dev == identity.st_dev
            and opened.st_ino == identity.st_ino
        ):
            swapped = True
            record.rename(record.with_suffix(".original"))
            record.write_bytes(b'{"stable":true}\n')
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", read_and_swap)

    # When: capture hashes the retained descriptor while the name is rebound.
    with pytest.raises(IsolationError, match="changed during inventory"):
        capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # Then: a mixed old-inode/new-name manifest is never returned.
    assert swapped


def _paths(manifest: JsonObject) -> set[str]:
    entries = cast("list[JsonObject]", manifest["entries"])
    return {cast("str", entry["relative_path"]) for entry in entries}


def _entry(manifest: JsonObject, relative_path: str) -> JsonObject:
    entries = cast("list[JsonObject]", manifest["entries"])
    matches = [
        entry for entry in entries if entry.get("relative_path") == relative_path
    ]
    assert len(matches) == 1
    return matches[0]
