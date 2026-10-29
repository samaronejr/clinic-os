from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from ops.testing import isolation_process_observation as observation
from ops.testing.isolation_common import IsolationError, JsonObject

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _proc_member(tmp_path: Path) -> tuple[Path, JsonObject]:
    root = tmp_path / "123"
    root.mkdir()
    fields = ["0"] * 50
    fields[0:4] = ["R", "100", "123", "123"]
    fields[19] = "456"
    (root / "stat").write_text("123 (synthetic worker) " + " ".join(fields))
    (root / "status").write_bytes(
        b"Uid:\t1000\t1000\t1000\t1000\nGid:\t100\t100\t100\t100\n"
    )
    (root / "cmdline").write_bytes(b"synthetic-python\0")
    executable = tmp_path / "synthetic-python"
    executable.write_bytes(b"synthetic executable")
    (root / "exe").symlink_to(executable)
    return root, {
        "argv_sha256": hashlib.sha256(b"synthetic-python\0").hexdigest(),
        "executable_realpath": str(executable),
        "gid": 100,
        "pgid": 123,
        "pid": 123,
        "ppid": 100,
        "sid": 123,
        "start_ticks": 456,
        "uid": 1000,
    }


def _during_read(monkeypatch: pytest.MonkeyPatch, mutate: Callable[[], None]) -> None:
    read_proc = observation._read_proc

    def read_and_mutate(path: Path) -> bytes:
        raw = read_proc(path)
        if path.name == "cmdline":
            mutate()
        return raw

    monkeypatch.setattr(observation, "_read_proc", read_and_mutate)


@pytest.mark.parametrize("field", [2, 3, *range(7, 22), *range(23, 53)])
def test_observation_accepts_nonidentity_stat_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int
) -> None:
    # Given: one process whose nonidentity stat field changes between samples.
    root, expected = _proc_member(tmp_path)

    def mutate() -> None:
        raw = (root / "stat").read_bytes()
        if field == 2:
            raw = raw.replace(b"(synthetic worker)", b"(renamed (worker))")
        else:
            prefix, _, tail = raw.rpartition(b")")
            fields = tail.split()
            fields[field - 3] = b"S" if field == 3 else b"1"
            raw = prefix + b") " + b" ".join(fields)
        (root / "stat").write_bytes(raw)

    _during_read(monkeypatch, mutate)

    # When: the native observer reads both samples.
    actual = observation.observe_process_member(123, tmp_path)

    # Then: volatile process activity does not change reserved identity.
    assert actual == expected


def test_observation_accepts_exe_link_metadata_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a replacement procfs link with the exact same executable target.
    root, expected = _proc_member(tmp_path)
    replacement = root / "replacement-exe"
    replacement.symlink_to(str(expected["executable_realpath"]))
    before = (root / "exe").lstat()
    after = replacement.lstat()
    assert before.st_ino != after.st_ino

    def mutate() -> None:
        replacement.replace(root / "exe")

    _during_read(monkeypatch, mutate)

    # When: the link inode and timestamps change during observation.
    actual = observation.observe_process_member(123, tmp_path)

    # Then: target identity, not the procfs link inode, authenticates execution.
    assert actual == expected


@pytest.mark.parametrize(
    ("field", "name"),
    [(1, "pid"), (4, "ppid"), (5, "pgid"), (6, "sid"), (22, "start_ticks")],
)
def test_observation_rejects_stat_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int, name: str
) -> None:
    # Given: the PID is substituted or its reserved topology/starttime changes.
    root, _ = _proc_member(tmp_path)

    def mutate() -> None:
        raw = (root / "stat").read_bytes()
        if field == 1:
            raw = raw.replace(b"123 (", b"124 (", 1)
        else:
            prefix, _, tail = raw.rpartition(b")")
            fields = tail.split()
            fields[field - 3] = str(int(fields[field - 3]) + 1).encode()
            raw = prefix + b") " + b" ".join(fields)
        (root / "stat").write_bytes(raw)

    _during_read(monkeypatch, mutate)

    # When/Then: a same-PID/different-starttime reuse also fails by named field.
    with pytest.raises(IsolationError, match=name):
        observation.observe_process_member(123, tmp_path)


@pytest.mark.parametrize("name", ["uid", "gid"])
def test_observation_rejects_effective_credential_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Given: effective credentials change without a different PID or starttime.
    root, _ = _proc_member(tmp_path)

    def mutate() -> None:
        status = root / "status"
        raw = status.read_bytes()
        old = b"Uid:\t1000\t1000" if name == "uid" else b"Gid:\t100\t100"
        status.write_bytes(raw.replace(old, old.rsplit(b"\t", 1)[0] + b"\t2000"))

    _during_read(monkeypatch, mutate)

    # When/Then: credentials are reauthenticated, with field names but no values.
    with pytest.raises(IsolationError, match=name):
        observation.observe_process_member(123, tmp_path)


@pytest.mark.parametrize("same_path", [False, True])
def test_observation_rejects_executable_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_path: bool
) -> None:
    # Given: an executable substitution, possibly keeping the target pathname.
    root, expected = _proc_member(tmp_path)
    executable = tmp_path / "replacement-python"
    executable.write_bytes(b"different synthetic executable")

    def mutate() -> None:
        if same_path:
            executable.replace(str(expected["executable_realpath"]))
        else:
            (root / "exe").unlink()
            (root / "exe").symlink_to(executable)

    _during_read(monkeypatch, mutate)

    # When/Then: both exec path drift and same-path inode replacement fail closed.
    field = "executable_inode" if same_path else "executable_realpath"
    with pytest.raises(IsolationError, match=field):
        observation.observe_process_member(123, tmp_path)
