"""Check that private cleanup is confined to its creation capability."""

import os
from pathlib import Path

import pytest
from ops.testing.migration_upgrade_custody import unlink_owned

from infra import wave_h_entry_worker


def test_unlink_refuses_a_directory_with_the_wrong_identity(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    marker = foreign / "marker"
    marker.write_bytes(b"foreign")
    before = marker.stat()
    expected = root.stat()
    descriptor = os.open(foreign, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(RuntimeError, match="private_run_identity_mismatch"):
            unlink_owned(
                descriptor,
                f"{expected.st_dev}:{expected.st_ino}",
                "marker",
                missing_ok=False,
            )
    finally:
        os.close(descriptor)
    assert os.path.samestat(before, marker.stat())
    assert marker.read_bytes() == b"foreign"


def test_unlink_removes_an_owned_link_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    marker = tmp_path / "marker"
    marker.write_bytes(b"foreign")
    link = root / "link"
    link.symlink_to(marker)
    descriptor = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        expected = os.fstat(descriptor)
        unlink_owned(
            descriptor, f"{expected.st_dev}:{expected.st_ino}", "link", missing_ok=False
        )
    finally:
        os.close(descriptor)
    assert not link.is_symlink()
    assert marker.read_bytes() == b"foreign"


def test_attestation_delete_refuses_a_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    marker = foreign / "verified.json"
    marker.write_bytes(b"foreign")
    before = marker.stat()
    created = owned.stat()
    monkeypatch.setenv(
        "UPGRADE_PRIVATE_RUN_IDENTITY", f"{created.st_dev}:{created.st_ino}"
    )
    with pytest.raises(RuntimeError, match="private_run_identity_mismatch"):
        wave_h_entry_worker._remove_attestation(marker)
    assert os.path.samestat(before, marker.stat())

    own_receipt = owned / "verified.json"
    own_receipt.write_bytes(b"mine")
    wave_h_entry_worker._remove_attestation(own_receipt)
    assert not own_receipt.exists()
    wave_h_entry_worker._remove_attestation(own_receipt)
