"""Nothing the rehearsal creates in its run directory may follow or reuse a name."""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ops.testing import migration_upgrade_custody as custody
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
    with pytest.raises(RuntimeError, match="private_tree_replaced"):
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


def _census_tree(root: Path, descriptor: int, identity: str) -> dict[str, str]:
    return {"base": mkdir_owned(descriptor, identity, "base")}


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(("base/inner", "/etc/passwd", None), id="nested-absolute"),
        pytest.param(("base/inner", "../tmp", None), id="nested-relative"),
        pytest.param(("base/.venv", "/etc", None), id="unrecorded-venv-link"),
        pytest.param(("base/inner", "fifo", "fifo"), id="special-file"),
    ],
)
def test_census_refuses_anything_the_run_did_not_create(
    run: tuple[Path, int, str], plant: tuple[str, str, str | None]
) -> None:
    root, descriptor, identity = run
    created = _census_tree(root, descriptor, identity)
    check_tree(descriptor, identity, created)
    name, target, special = plant
    if special == "fifo":
        os.mkfifo(root / name)
    else:
        (root / name).symlink_to(target)

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)


def _stat_key(path: str) -> tuple[int, int]:
    found = Path(path).stat()
    return found.st_dev, found.st_ino


def _venv_tree(run: tuple[Path, int, str]) -> tuple[Path, int, str, dict[str, str]]:
    root, descriptor, identity = run
    created = _census_tree(root, descriptor, identity)
    (root / "base/.venv/bin").mkdir(parents=True)
    (root / "uv-cache").mkdir()
    created["uv-cache"] = _identity(root / "uv-cache")
    return root, descriptor, identity, created


def test_census_accepts_recorded_links_and_links_resolving_inside_the_run(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity, created = _venv_tree(run)
    (root / "base/alias").symlink_to("tool.sh")
    (root / "uv-cache/archive").write_bytes(b"x")
    (root / "uv-cache/entry").symlink_to(root / "uv-cache/archive")
    (root / "base/.venv/lib64").symlink_to("../../uv-cache/archive")

    check_tree(descriptor, identity, created, {"base/alias": "tool.sh"})

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry: base/alias"):
        check_tree(descriptor, identity, created, {})


@pytest.mark.parametrize("name", ["python3", "python", "python3.13", "interpreter-v4"])
def test_census_validates_links_by_inode_and_never_by_name(
    run: tuple[Path, int, str], tmp_path: Path, name: str
) -> None:
    """A link to a foreign file named python3 is refused, whatever it is called."""
    root, descriptor, identity, created = _venv_tree(run)
    foreign = tmp_path / "foreign" / "python3"
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign")
    (root / "base/.venv/bin" / name).symlink_to(foreign)

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)


def test_census_accepts_a_link_only_when_its_exact_inode_was_recorded(
    run: tuple[Path, int, str], tmp_path: Path
) -> None:
    root, descriptor, identity, created = _venv_tree(run)
    recorded, other = tmp_path / "recorded", tmp_path / "other"
    recorded.write_bytes(b"the interpreter")
    other.write_bytes(b"another file")
    link = root / "base/.venv/bin/python3"
    link.symlink_to(recorded)

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)
    allowed = frozenset({_stat_key(str(recorded))})
    check_tree(descriptor, identity, created, allowed=allowed)
    link.unlink()
    link.symlink_to(other)
    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created, allowed=allowed)


@pytest.mark.parametrize(
    "target",
    ["/proc/self/fd/0", "/proc/self/fd/2", "/dev/fd/1", "/dev/null", "/dev/stdout"],
)
@pytest.mark.parametrize("where", ["uv-cache/interpreter-v4", "base/.venv/bin/python3"])
def test_census_refuses_links_to_proc_and_dev(
    run: tuple[Path, int, str], target: str, where: str
) -> None:
    root, descriptor, identity, created = _venv_tree(run)
    (root / where).symlink_to(target)

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)


def test_census_refuses_a_link_that_does_not_resolve(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity, created = _venv_tree(run)
    (root / "uv-cache/interpreter-v4").symlink_to(root / "uv-cache/never-made")

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry"):
        check_tree(descriptor, identity, created)


def test_extraction_reports_the_links_the_archive_carried(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity = run
    links: dict[str, str] = {}
    stream = _tar(_directory("pkg"), _link("pkg/alias", "tool.sh"), _file("top.txt"))

    extract_owned(descriptor, identity, "base", stream, links)

    assert links == {"base/pkg/alias": "tool.sh"}
    check_tree(descriptor, identity, {"base": _identity(root / "base")}, links)


def test_census_accepts_hard_links_whose_every_name_is_inside_the_tree(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity = run
    created = _census_tree(root, descriptor, identity)
    (root / "base/tool").write_bytes(b"x")
    (root / "base/other").hardlink_to(root / "base/tool")

    check_tree(descriptor, identity, created)


def test_census_refuses_a_file_that_has_a_name_outside_the_tree(
    run: tuple[Path, int, str], tmp_path: Path
) -> None:
    root, descriptor, identity = run
    created = _census_tree(root, descriptor, identity)
    foreign = tmp_path / "foreign-file"
    foreign.write_bytes(b"foreign")
    (root / "base/linked").hardlink_to(foreign)

    with pytest.raises(RuntimeError, match="private_tree_foreign_entry: base/linked"):
        check_tree(descriptor, identity, created)


def _sealed(
    run: tuple[Path, int, str],
) -> tuple[Path, int, str, dict[str, list[object]]]:
    root, descriptor, identity = run
    created = _census_tree(root, descriptor, identity)
    (root / "base/a").write_bytes(b"1234")
    (root / "base/b").symlink_to("a")
    check_tree(descriptor, identity, created)
    return root, descriptor, identity, custody.snapshot(descriptor, identity)


def test_a_sealed_tree_compares_equal_to_itself_and_allows_named_additions(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity, sealed = _sealed(run)

    custody.unchanged_since(sealed, custody.snapshot(descriptor, identity))
    (root / "verdict.json").write_bytes(b"{}")
    custody.unchanged_since(
        sealed, custody.snapshot(descriptor, identity), frozenset({"verdict.json"})
    )
    with pytest.raises(RuntimeError, match=r"private_tree_changed: new verdict\.json"):
        custody.unchanged_since(sealed, custody.snapshot(descriptor, identity))


def test_a_sealed_tree_shows_bytes_rewritten_in_place_at_the_same_size(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity, sealed = _sealed(run)
    (root / "base/a").write_bytes(b"5678")

    with pytest.raises(RuntimeError, match="private_tree_changed: base/a"):
        custody.unchanged_since(sealed, custody.snapshot(descriptor, identity))


def test_a_sealed_tree_shows_an_object_replaced_by_an_identical_copy(
    run: tuple[Path, int, str],
) -> None:
    root, descriptor, identity, sealed = _sealed(run)
    # A second inode must exist before the first is released, or tmpfs reuses it.
    (root / "base/copy").write_bytes(b"1234")
    (root / "base/copy").replace(root / "base/a")

    with pytest.raises(RuntimeError, match="private_tree_changed: base/a"):
        custody.unchanged_since(sealed, custody.snapshot(descriptor, identity))


def test_a_sealed_tree_shows_a_removed_object(run: tuple[Path, int, str]) -> None:
    root, descriptor, identity, sealed = _sealed(run)
    (root / "base/b").unlink()

    with pytest.raises(RuntimeError, match="private_tree_changed: missing base/b"):
        custody.unchanged_since(sealed, custody.snapshot(descriptor, identity))


FOREIGN_MOUNT_PROBE = """
import os, subprocess, sys
from ops.testing.migration_upgrade_custody import check_tree

root, mount = sys.argv[1], sys.argv[2]
descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
found = os.fstat(descriptor)
identity = f"{found.st_dev}:{found.st_ino}"
os.mkdir(os.path.join(root, "mnt"))
subprocess.run([mount, "-t", "tmpfs", "tmpfs", os.path.join(root, "mnt")], check=True)
try:
    check_tree(descriptor, identity, {})
except RuntimeError as refusal:
    print(refusal)
"""


@pytest.mark.skipif(
    shutil.which("unshare") is None or shutil.which("mount") is None,
    reason="needs unshare and mount to make a mount in a private namespace",
)
def test_a_mount_visible_to_the_census_under_the_run_is_refused(
    run: tuple[Path, int, str],
) -> None:
    """A filesystem mounted under the run, in the census's own namespace, is refused.

    This is the only mount the census can judge. A bind made inside a child's own
    mount namespace is invisible to it; that route is the strict xfail in the
    sandbox tests and rests on the Landlock policy alone.
    """
    root, _, _ = run
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    assert unshare is not None
    assert mount is not None

    result = subprocess.run(  # noqa: S603 - fixed probe in a private namespace
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            sys.executable,
            "-c",
            FOREIGN_MOUNT_PROBE,
            str(root),
            mount,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "private_tree_foreign_mount: mnt"


def _committed(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "ops/testing").mkdir(parents=True)
    (repo / "ops/db").mkdir()
    (repo / "ops/testing/helper.py").write_text("print('committed')\n")
    (repo / "ops/testing/run.sh").write_text("#!/bin/sh\n")
    (repo / "ops/testing/run.sh").chmod(0o755)
    (repo / "ops/db/bootstrap.sql").write_text("SELECT 1;\n")
    git = shutil.which("git")
    assert git is not None
    commit_arguments = ["-c", "commit.gpgsign=false", "-c", "user.name=t"]
    for arguments in (
        ["init", "-q"],
        ["add", "-A"],
        [*commit_arguments, "-c", "user.email=t@example.invalid", "commit", "-qm", "c"],
    ):
        subprocess.run([git, "-C", str(repo), *arguments], check=True)  # noqa: S603
    head = subprocess.run(  # noqa: S603
        [git, "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo, head.stdout.strip()


SEAL_NONCE = "e" * 32


def _seal(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Commit helpers, seal them, and return the repo, sealed root, identity, commit."""
    repo, commit = _committed(tmp_path)
    sealed = tmp_path / f"migration-upgrade-sealed.{SEAL_NONCE}"
    identity = custody.seal_helpers(str(repo), commit, str(sealed))
    return repo, sealed, identity, commit


def _verify(repo: Path, sealed: Path, identity: str, nonce: str = SEAL_NONCE) -> None:
    custody.verify_helpers(str(repo), str(sealed), identity, nonce)


def test_sealed_helpers_are_the_committed_bytes_whatever_the_checkout_holds(
    tmp_path: Path,
) -> None:
    repo, commit = _committed(tmp_path)
    helper = repo / "ops/testing/helper.py"
    helper.write_text("print('edited')\n")
    sealed = tmp_path / f"migration-upgrade-sealed.{SEAL_NONCE}"

    identity = custody.seal_helpers(str(repo), commit, str(sealed))

    assert (sealed / "ops/testing/helper.py").read_text() == "print('committed')\n"
    assert (sealed / "ops/db/bootstrap.sql").read_text() == "SELECT 1;\n"
    assert os.access(sealed / "ops/testing/run.sh", os.X_OK)
    assert stat.S_IMODE((sealed / "ops").stat().st_mode) == 0o700
    with pytest.raises(RuntimeError, match=r"helpers_changed: ops/testing/helper\.py"):
        _verify(repo, sealed, identity)
    helper.write_text("print('committed')\n")
    _verify(repo, sealed, identity)


def test_a_helper_swapped_for_a_link_after_sealing_is_refused(tmp_path: Path) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)
    helper = repo / "ops/testing/helper.py"
    copy = tmp_path / "identical-copy.py"
    copy.write_text("print('committed')\n")
    helper.unlink()
    helper.symlink_to(copy)

    with pytest.raises(RuntimeError, match=r"helpers_changed: ops/testing/helper\.py"):
        _verify(repo, sealed, identity)


def test_a_sealed_root_replaced_by_an_identical_copy_is_refused(tmp_path: Path) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)
    moved = tmp_path / "moved"
    sealed.rename(moved)
    shutil.copytree(moved, sealed)

    with pytest.raises(RuntimeError, match="helpers_root_replaced"):
        _verify(repo, sealed, identity)


def test_a_sealed_root_replaced_by_a_link_is_refused(tmp_path: Path) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)
    moved = tmp_path / "moved"
    sealed.rename(moved)
    sealed.symlink_to(moved)

    with pytest.raises(RuntimeError, match="helpers_root_replaced"):
        _verify(repo, sealed, identity)


def test_a_sealed_root_under_another_nonce_is_refused(tmp_path: Path) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)

    with pytest.raises(RuntimeError, match="helpers_root_unbound"):
        _verify(repo, sealed, identity, nonce="f" * 32)


@pytest.mark.parametrize(
    "plant",
    ["evil.py", "ops/evil.py", "ops/other/x.py", "ops/testing/extra.py"],
)
def test_an_entry_the_sealing_did_not_plan_is_refused(
    tmp_path: Path, plant: str
) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)
    (sealed / plant).parent.mkdir(parents=True, exist_ok=True)
    (sealed / plant).write_text("print('planted')\n")

    with pytest.raises(RuntimeError, match=r"helpers_foreign_entry|helpers_changed"):
        _verify(repo, sealed, identity)


def test_a_link_planted_inside_the_sealed_root_is_refused(tmp_path: Path) -> None:
    repo, sealed, identity, _ = _seal(tmp_path)
    (sealed / "ops/testing/link.py").symlink_to(sealed / "ops/testing/helper.py")

    with pytest.raises(RuntimeError, match="helpers_foreign_entry"):
        _verify(repo, sealed, identity)


def test_an_object_that_does_not_hash_to_its_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit = _committed(tmp_path)
    real = subprocess.run

    def forged(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        if command[-2] == "blob":
            return subprocess.CompletedProcess(command, 0, stdout=b"forged")
        return real(command, **options)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", forged)

    with pytest.raises(RuntimeError, match="helper_seal_object_mismatch"):
        custody.seal_helpers(str(repo), commit, str(tmp_path / "sealed"))


def test_capture_reads_one_regular_file_exactly(run: tuple[Path, int, str]) -> None:
    root, descriptor, identity = run
    (root / "verified.json").write_bytes(b'{"a": 1}\n')

    assert custody.capture_file(descriptor, identity, "verified.json") == b'{"a": 1}\n'


def test_capture_refuses_a_link_and_a_second_name(
    run: tuple[Path, int, str], foreign: tuple[Path, Path]
) -> None:
    root, descriptor, identity = run
    _, marker = foreign
    (root / "linked.json").symlink_to(marker)
    (root / "hard.json").hardlink_to(marker)

    with pytest.raises(OSError, match="Too many levels of symbolic links"):
        custody.capture_file(descriptor, identity, "linked.json")
    with pytest.raises(
        RuntimeError, match=r"private_capture_foreign_entry: hard\.json"
    ):
        custody.capture_file(descriptor, identity, "hard.json")


def _publish(
    directory: Path, result: str, held: str, differential: str = ""
) -> dict[str, object]:
    custody.publish_verdict(
        [
            str(directory),
            "b" * 40,
            "db",
            result,
            "stage",
            "run1",
            "c" * 40,
            "1.5",
            differential,
            "n" * 32,
        ],
        held,
    )
    verdict: dict[str, object] = json.loads(
        (directory / "verdict.json").read_text(encoding="utf-8")
    )
    return verdict


ATTESTATION = json.dumps(
    {"schema_version": 1, "result": "PASS", "base_sha": "b" * 40, "upgrade_db": "db"}
)
FAILED_REPORT = json.dumps(
    {
        "result": "FAIL",
        "projection_cases": 5,
        "probe_window_cases": 2,
        "differences": ["x"],
        "base_reject_lane_pass_pairs": [],
    }
)


def test_a_pass_is_built_from_the_held_bytes_alone(tmp_path: Path) -> None:
    published = tmp_path / "published"
    published.mkdir()

    verdict = _publish(published, "PASS", ATTESTATION + custody.RECORD_SEPARATOR)

    assert verdict["result"] == "PASS"
    assert verdict["run_nonce"] == "n" * 32
    assert "differential" not in verdict
    assert [entry.name for entry in published.iterdir()] == ["verdict.json"]


def test_a_fail_names_its_stage_and_carries_the_held_differential(
    tmp_path: Path,
) -> None:
    held = custody.RECORD_SEPARATOR + FAILED_REPORT

    verdict = _publish(tmp_path, "FAIL", held, differential="1")

    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "stage"
    assert verdict["differential"] == json.loads(FAILED_REPORT)


def test_a_pass_without_a_passing_differential_is_refused(tmp_path: Path) -> None:
    held = ATTESTATION + custody.RECORD_SEPARATOR + FAILED_REPORT

    with pytest.raises(RuntimeError, match="differential passing report is missing"):
        _publish(tmp_path, "PASS", held, differential="1")
