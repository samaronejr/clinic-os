"""Check that private cleanup is confined to its creation capability."""

import os
from pathlib import Path

import pytest
from ops.testing.migration_upgrade_custody import unlink_owned


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
