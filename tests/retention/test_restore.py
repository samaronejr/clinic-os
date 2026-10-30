"""Pure filesystem and authenticated restore-boundary tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest
from apps.retention.storage import SyntheticPurgeStorage
from ops.testing import restore_purge
from ops.testing.restore_contract import RestoreContractError
from ops.testing.restore_purge import reapply_purge_tombstones

if TYPE_CHECKING:
    from pathlib import Path

KEY = "c" * 64


@dataclass
class RestoredDatabase:
    targets: list[dict[str, str]]
    statements: list[str] = field(default_factory=list)

    def sql(self, statement: str) -> str:
        self.statements.append(statement)
        return json.dumps(self.targets)


def test_restore_erases_restored_audio_and_pending_marker_before_exposure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    storage = SyntheticPurgeStorage(root)
    storage.put(KEY, b"synthetic ciphertext")
    storage.mark_pending(KEY, "00000000-0000-4000-8000-000000000000")
    location = sha256(f"filesystem:{root.resolve()}".encode()).hexdigest()
    database = RestoredDatabase([{"key": KEY, "location": location}])
    assert reapply_purge_tombstones(database, "[]", root) == 1
    assert storage.absent(KEY)
    assert len(database.statements) == 1
    assert "retention_restore_tombstones" in database.statements[0]


def test_restore_rejects_a_wrong_root_without_claiming_target_absence(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    storage = SyntheticPurgeStorage(actual)
    storage.put(KEY, b"synthetic ciphertext")
    location = sha256(f"filesystem:{actual.resolve()}".encode()).hexdigest()
    database = RestoredDatabase([{"key": KEY, "location": location}])

    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", tmp_path / "unrelated")

    assert not storage.absent(KEY)
    assert not (tmp_path / "unrelated").exists()


def test_restore_validates_every_namespace_before_deleting_any_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    storage = SyntheticPurgeStorage(root)
    storage.put(KEY, b"synthetic ciphertext")
    location = sha256(f"filesystem:{root.resolve()}".encode()).hexdigest()
    database = RestoredDatabase(
        [
            {"key": KEY, "location": location},
            {"key": "d" * 64, "location": "f" * 64},
        ]
    )

    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", root)

    assert not storage.absent(KEY)


def test_restore_without_explicit_media_root_fails_closed() -> None:
    database = RestoredDatabase([{"key": KEY, "location": "f" * 64}])
    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", None)


@pytest.mark.parametrize(
    "targets",
    [
        [{"key": "../clinical", "location": "f" * 64}],
        [{"key": KEY, "location": ""}],
        [{"key": KEY, "location": "f" * 64, "extra": "untrusted"}],
    ],
)
def test_restore_rejects_malformed_manifests_before_any_file_mutation(
    tmp_path: Path, targets: list[dict[str, str]]
) -> None:
    database = RestoredDatabase(targets)
    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", tmp_path / "media")
    assert not (tmp_path / "media").exists()


def test_unknown_current_tombstone_ledger_never_reaches_the_database() -> None:
    database = RestoredDatabase([])
    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "{}", None)
    assert database.statements == []


def test_restore_copy_binding_rejects_an_unrelated_destination(tmp_path: Path) -> None:
    source = tmp_path / "media-source"
    source_storage = SyntheticPurgeStorage(source)
    source_storage.put(KEY, b"synthetic ciphertext")
    destination = tmp_path / "media-restored"
    destination.mkdir(mode=0o700)
    binding = restore_purge.restore_media_copy(
        source=source, destination=destination, work_dir=tmp_path
    )
    location = sha256(f"filesystem:{source.resolve()}".encode()).hexdigest()
    database = RestoredDatabase([{"key": KEY, "location": location}])

    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(
            database, "[]", tmp_path / "unrelated", media_binding=binding
        )

    assert not SyntheticPurgeStorage(destination).absent(KEY)
    assert not source_storage.absent(KEY)
    assert not (tmp_path / "unrelated").exists()


def test_restore_copy_binding_rejects_a_foreign_manifest_namespace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "media-source"
    SyntheticPurgeStorage(source).put(KEY, b"synthetic ciphertext")
    destination = tmp_path / "media-restored"
    destination.mkdir(mode=0o700)
    binding = restore_purge.restore_media_copy(
        source=source, destination=destination, work_dir=tmp_path
    )
    database = RestoredDatabase([{"key": KEY, "location": "f" * 64}])

    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", destination, media_binding=binding)

    assert not SyntheticPurgeStorage(destination).absent(KEY)


def test_restore_copy_binding_rejects_a_replaced_destination(tmp_path: Path) -> None:
    source = tmp_path / "media-source"
    SyntheticPurgeStorage(source).put(KEY, b"synthetic ciphertext")
    destination = tmp_path / "media-restored"
    destination.mkdir(mode=0o700)
    binding = restore_purge.restore_media_copy(
        source=source, destination=destination, work_dir=tmp_path
    )
    # Keep the original inode alive so the replacement cannot reuse it.
    destination.rename(tmp_path / "original-copy")
    replacement = SyntheticPurgeStorage(destination)
    replacement.put(KEY, b"unrelated synthetic ciphertext")
    location = sha256(f"filesystem:{source.resolve()}".encode()).hexdigest()
    database = RestoredDatabase([{"key": KEY, "location": location}])

    with pytest.raises(RestoreContractError):
        reapply_purge_tombstones(database, "[]", destination, media_binding=binding)

    assert not replacement.absent(KEY)
    assert not SyntheticPurgeStorage(tmp_path / "original-copy").absent(KEY)


@pytest.mark.parametrize("invalid", ["foreign", "symlink", "nonempty"])
def test_restore_copy_requires_the_reserved_fresh_isolated_root(
    tmp_path: Path, invalid: str
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir(mode=0o700)
    source = tmp_path / "media-source"
    SyntheticPurgeStorage(source).put(KEY, b"synthetic ciphertext")
    destination = work_dir / "media-restored"
    if invalid == "foreign":
        destination = tmp_path / "media-restored"
    if invalid == "symlink":
        destination.symlink_to(source, target_is_directory=True)
    else:
        destination.mkdir(mode=0o700)
        if invalid == "nonempty":
            (destination / KEY).write_bytes(b"unrelated synthetic ciphertext")

    with pytest.raises(RestoreContractError):
        restore_purge.restore_media_copy(
            source=source, destination=destination, work_dir=work_dir
        )

    assert not SyntheticPurgeStorage(source).absent(KEY)
