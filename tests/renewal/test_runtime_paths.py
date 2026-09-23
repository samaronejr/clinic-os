from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import pytest
from ops.testing import runtime_paths
from ops.testing.isolation_common import IsolationError, write_no_replace


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    return root


@pytest.mark.parametrize(
    "purpose",
    [
        "browser-probe",
        "postgres",
        "image",
        "restore",
        "ca-export",
        "materializer",
        "https",
    ],
)
def test_each_purpose_uses_two_independent_roots(tmp_path: Path, purpose: str) -> None:
    # Given: independent private execution roots.
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir(mode=0o700)
    # When: each run allocates and publishes a real immutable file.
    for root in roots:
        with runtime_paths.runtime_directory(root, purpose=purpose) as work:
            write_no_replace(work / "spec.json", b'{"input":7}\n', mode=0o400)
            # Then: exact bytes and mode stay within the selected root.
            assert work.parent == root
            assert work.is_absolute()
            assert work == work.resolve(strict=True)
            assert stat.S_IMODE(work.stat().st_mode) == 0o700
            assert (work / "spec.json").read_bytes() == b'{"input":7}\n'
            assert stat.S_IMODE((work / "spec.json").stat().st_mode) == 0o400
        assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "purpose", ["", ".", "..", "../escape", "/absolute", "a/b", "a\\b", "a\x00b"]
)
def test_invalid_purpose_cannot_escape(run_root: Path, purpose: str) -> None:
    # Given: a caller-controlled purpose that is not a single safe component.
    # When: the public directory boundary receives it.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(run_root, purpose=purpose),
    ):
        pytest.fail("unsafe purpose was accepted")
    # Then: no child was allocated.
    assert list(run_root.iterdir()) == []


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750, 0o770, 0o711])
def test_insecure_root_mode_preserves_sentinel(run_root: Path, mode: int) -> None:
    # Given: an occupied root with an incompatible access mode.
    sentinel = run_root / "sentinel"
    _ = sentinel.write_bytes(b"preserve")
    run_root.chmod(mode)
    # When: the root is opened for runtime allocation.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(run_root, purpose="test"),
    ):
        pytest.fail("insecure root was accepted")
    # Then: neither the occupant nor the existing mode is repaired or removed.
    assert sentinel.read_bytes() == b"preserve"
    assert stat.S_IMODE(run_root.stat().st_mode) == mode


@pytest.mark.parametrize("kind", ["root", "ancestor", "dangling"])
def test_symlinked_root_chain_is_rejected(tmp_path: Path, kind: str) -> None:
    # Given: an alias to a private directory or an absent target.
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    _ = (target / "sentinel").write_bytes(b"preserve")
    link = tmp_path / "link"
    link.symlink_to(target if kind != "dangling" else tmp_path / "missing")
    root = link
    if kind == "ancestor":
        (target / "nested").mkdir(mode=0o700)
        root = link / "nested"
    # When: any component of the selected root is a symlink.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(root, purpose="test"),
    ):
        pytest.fail("symlink traversal was accepted")
    # Then: the link and target contents survive unchanged.
    assert link.is_symlink()
    assert (target / "sentinel").read_bytes() == b"preserve"


@pytest.mark.parametrize("kind", ["relative", "traversal", "file", "missing"])
def test_invalid_root_is_rejected_without_creation(tmp_path: Path, kind: str) -> None:
    # Given: a root that cannot identify an existing canonical private directory.
    occupied = tmp_path / "occupied"
    _ = occupied.write_bytes(b"preserve")
    roots = {
        "relative": Path("relative"),
        "traversal": tmp_path / ".." / tmp_path.name,
        "file": occupied,
        "missing": tmp_path / "absent",
    }
    # When: the public directory boundary receives it.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(roots[kind], purpose="test"),
    ):
        pytest.fail("invalid root was accepted")
    # Then: existing occupants remain and missing roots are not created.
    assert occupied.read_bytes() == b"preserve"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("identity_function", ["geteuid", "getegid"])
def test_foreign_root_identity_is_rejected(
    run_root: Path, monkeypatch: pytest.MonkeyPatch, identity_function: str
) -> None:
    # Given: actual file ownership differs from the executing identity.
    value = os.geteuid() if identity_function == "geteuid" else os.getegid()
    monkeypatch.setattr(os, identity_function, lambda: value + 1)
    # When: the owner or group is foreign to this executor.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(run_root, purpose="test"),
    ):
        pytest.fail("foreign root was accepted")
    # Then: no resource is claimed or removed.
    assert list(run_root.iterdir()) == []


def test_exception_removes_only_allocated_child(run_root: Path) -> None:
    # Given: an existing sibling belonging to another operation.
    sibling = run_root / "sentinel"
    _ = sibling.write_bytes(b"preserve")

    # When: the operation fails after writing nested temporary output.
    def failing_operation() -> None:
        with runtime_paths.runtime_directory(run_root, purpose="test") as work:
            (work / "nested").mkdir(mode=0o700)
            _ = (work / "nested" / "output").write_bytes(b"temporary")
            raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        failing_operation()
    # Then: only the pre-existing sibling remains.
    assert list(run_root.iterdir()) == [sibling]
    assert sibling.read_bytes() == b"preserve"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_child_is_preserved(run_root: Path, replacement: str) -> None:
    # Given: another resource takes over the allocated pathname during the body.
    held = run_root / "held"

    def substituted_operation() -> None:
        with runtime_paths.runtime_directory(run_root, purpose="test") as work:
            _ = work.rename(held)
            if replacement == "directory":
                work.mkdir(mode=0o700)
            else:
                work.symlink_to(held, target_is_directory=True)
            _ = (held / "sentinel").write_bytes(b"preserve")

    with pytest.raises(IsolationError):
        substituted_operation()
    # Then: cleanup refuses both the substituted name and the renamed original.
    assert len(list(run_root.iterdir())) == 2
    assert (held / "sentinel").read_bytes() == b"preserve"


def test_replaced_root_is_preserved(run_root: Path) -> None:
    # Given: the selected root is replaced while a child is active.
    held = run_root.with_name("held")

    def substituted_operation() -> None:
        with runtime_paths.runtime_directory(run_root, purpose="test") as work:
            _ = (work / "sentinel").write_bytes(b"preserve")
            _ = run_root.rename(held)
            run_root.mkdir(mode=0o700)
            _ = (run_root / "foreign").write_bytes(b"untouched")

    with pytest.raises(IsolationError):
        substituted_operation()
    # Then: neither root is recursively removed.
    [work] = held.iterdir()
    assert (work / "sentinel").read_bytes() == b"preserve"
    assert (run_root / "foreign").read_bytes() == b"untouched"


def test_occupied_child_is_not_reused(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an occupied pathname colliding with the generated unique child name.
    occupied = run_root / ("test-" + "a" * 32)
    occupied.mkdir(mode=0o700)
    _ = (occupied / "sentinel").write_bytes(b"preserve")

    def collide(_size: int) -> str:
        return "a" * 32

    monkeypatch.setattr(secrets, "token_hex", collide)
    # When: exclusive allocation encounters the occupied name.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(run_root, purpose="test"),
    ):
        pytest.fail("occupied child was reused")
    # Then: no fallback adopts or destroys the occupied resource.
    assert (occupied / "sentinel").read_bytes() == b"preserve"


def test_root_replacement_during_allocation_cannot_write_into_replacement(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the pathname is replaced after root validation but before mkdir.
    original_mkdir = os.mkdir
    held = run_root.with_name("held")

    def replace_before_mkdir(
        path: str, mode: int, *, dir_fd: int | None = None
    ) -> None:
        _ = run_root.rename(held)
        original_mkdir(run_root, 0o700)
        _ = (run_root / "sentinel").write_bytes(b"preserve")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", replace_before_mkdir)
    # When: allocation uses the held descriptor and then revalidates its pathname.
    with (
        pytest.raises(IsolationError),
        runtime_paths.runtime_directory(run_root, purpose="test"),
    ):
        pytest.fail("replaced root was yielded")
    # Then: allocation is confined to the original root; foreign entries survive.
    assert list(run_root.iterdir()) == [run_root / "sentinel"]
    assert (run_root / "sentinel").read_bytes() == b"preserve"
    [allocated] = held.iterdir()
    assert allocated.is_dir()
    assert list(allocated.iterdir()) == []
