"""Nothing the rehearsal creates in its run directory may follow or reuse a name."""

import io
import os
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ops.testing.migration_upgrade_custody import (
    check_tree,
    extract_owned,
    mkdir_owned,
    require_empty,
    symlink_owned,
    write_replacing,
)

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _identity(path: Path) -> str:
    identity = path.stat()
    return f"{identity.st_dev}:{identity.st_ino}"


@pytest.fixture
def run(tmp_path: Path) -> Iterator[tuple[Path, int, str]]:
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    descriptor = os.open(root, DIRECTORY)
    yield root, descriptor, _identity(root)
    os.close(descriptor)


@pytest.fixture
def foreign(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "foreign-dir"
    directory.mkdir()
    marker = tmp_path / "foreign-file"
    marker.write_bytes(b"foreign\n")
    return directory, marker


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def _file(
    name: str, data: bytes = b"x", mode: int = 0o644
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, data


def _directory(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


def _link(
    name: str, target: str, kind: bytes = tarfile.SYMTYPE
) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _unchanged(foreign: tuple[Path, Path]) -> None:
    directory, marker = foreign
    assert list(directory.iterdir()) == []
    assert marker.read_bytes() == b"foreign\n"


def test_extraction_creates_only_new_entries(run: tuple[Path, int, str]) -> None:
    root, descriptor, identity = run
    stream = _tar(
        _directory("pkg"),
        _file("pkg/tool.sh", b"#!/bin/sh\n", 0o755),
        _file("top.txt"),
        _link("pkg/alias", "tool.sh"),
    )

    created = extract_owned(descriptor, identity, "base", stream)

    assert created == _identity(root / "base")
    assert (root / "base/pkg/tool.sh").read_bytes() == b"#!/bin/sh\n"
    assert os.access(root / "base/pkg/tool.sh", os.X_OK)
    assert (root / "base/pkg/alias").is_symlink()


@pytest.mark.parametrize("plant", ["directory_link", "file_link", "directory", "file"])
def test_extraction_refuses_every_preexisting_name(
    run: tuple[Path, int, str], foreign: tuple[Path, Path], plant: str
) -> None:
    root, descriptor, identity = run
    directory, marker = foreign
    name = root / "base"
    if plant == "directory_link":
        name.symlink_to(directory)
    elif plant == "file_link":
        name.symlink_to(marker)
    elif plant == "directory":
        name.mkdir()
    else:
        name.write_bytes(b"planted")

    with pytest.raises(FileExistsError):
        extract_owned(descriptor, identity, "base", _tar(_file("a.txt")))

    _unchanged(foreign)


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(lambda: [_file("/escaped.txt")], id="absolute"),
        pytest.param(lambda: [_file("../escaped.txt")], id="parent"),
        pytest.param(lambda: [_file("pkg/../../escaped.txt")], id="nested-parent"),
        pytest.param(lambda: [_link("link", "/etc")], id="absolute-symlink"),
        pytest.param(
            lambda: [_link("link", "../../foreign-dir")], id="escaping-symlink"
        ),
        pytest.param(lambda: [_link("link", "target", tarfile.LNKTYPE)], id="hardlink"),
        pytest.param(
            lambda: [_directory("sub"), _link("hop", "sub"), _file("hop/inner.txt")],
            id="write-through-member-symlink",
        ),
    ],
)
def test_extraction_refuses_hostile_members(
    run: tuple[Path, int, str],
    foreign: tuple[Path, Path],
    hostile: Callable[[], list[tuple[tarfile.TarInfo, bytes | None]]],
) -> None:
    root, descriptor, identity = run

    with pytest.raises((RuntimeError, tarfile.TarError, OSError)):
        extract_owned(descriptor, identity, "base", _tar(*hostile()))

    _unchanged(foreign)
    assert not (root.parent / "escaped.txt").exists()
    assert not (root / "escaped.txt").exists()
    assert not (root / "base/sub/inner.txt").exists()


def test_symlink_creation_refuses_every_existing_name(
    run: tuple[Path, int, str], foreign: tuple[Path, Path]
) -> None:
    root, descriptor, identity = run
    directory, marker = foreign
    child = mkdir_owned(descriptor, identity, "base")
    for name, target in ((".venv", directory), (".venv-file", marker)):
        (root / "base" / name).symlink_to(target)
        with pytest.raises(FileExistsError):
            symlink_owned(str(root / "base"), child, name, "/somewhere")
    with pytest.raises(RuntimeError, match="private_run_identity_mismatch"):
        symlink_owned(str(root / "base"), "0:0", "fresh", "/somewhere")
    _unchanged(foreign)


def test_empty_check_refuses_a_directory_that_gained_an_entry(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity = run
    child = mkdir_owned(descriptor, identity, "wave")
    require_empty(str(root / "wave"), child)
    (root / "wave" / ".git").write_bytes(b"planted")
    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        require_empty(str(root / "wave"), child)


def test_tree_check_refuses_links_and_replaced_children(
    run: tuple[Path, int, str], foreign: tuple[Path, Path]
) -> None:
    root, descriptor, identity = run
    directory, _ = foreign
    created = {
        name: mkdir_owned(descriptor, identity, name) for name in ("tmp", "uv-cache")
    }
    check_tree(descriptor, identity, created)

    (root / "tmp").rename(root / "tmp.moved")
    (root / "tmp").symlink_to(directory)
    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)

    (root / "tmp").unlink()
    (root / "tmp").mkdir()
    with pytest.raises(RuntimeError, match="private_tree_replaced"):
        check_tree(descriptor, identity, created)
    _unchanged(foreign)


def test_child_directory_creation_refuses_a_planted_name(
    run: tuple[Path, int, str], foreign: tuple[Path, Path]
) -> None:
    root, descriptor, identity = run
    (root / "native").symlink_to(foreign[0])
    with pytest.raises(FileExistsError):
        mkdir_owned(descriptor, identity, "native")
    _unchanged(foreign)


def test_replaced_receipt_never_writes_through_a_foreign_link(
    tmp_path: Path, foreign: tuple[Path, Path]
) -> None:
    _, marker = foreign
    receipt = tmp_path / "ai-assertions.json"
    receipt.symlink_to(marker)

    write_replacing(receipt, "{}\n")

    assert marker.read_bytes() == b"foreign\n"
    assert not receipt.is_symlink()
    assert receipt.read_text(encoding="utf-8") == "{}\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
