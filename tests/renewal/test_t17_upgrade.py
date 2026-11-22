"""Exercise the owner-host T17 publication contract without Docker or PostgreSQL."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest
from ops.testing import t17_upgrade
from ops.testing.isolation_archive_tree import move_no_replace
from ops.testing.t17_upgrade import UpgradeHost, UpgradePlan, run

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ops.testing.t17_upgrade import DatabaseIdentity


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 - disposable test repository.
        ["git", "-C", str(repo), *args],  # noqa: S607
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "config", "user.name", "Synthetic")
    for msg in ("original", "replacement"):
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", msg)
    replacement = _git(repo, "rev-parse", "HEAD")
    original = _git(repo, "rev-parse", "HEAD^")
    _git(repo, "switch", "--quiet", "--detach", original)
    return repo, original, replacement


def _host(*, postgres_removed: bool = True) -> UpgradeHost:
    @contextmanager
    def lease(
        runtime: Path, label: str
    ) -> Iterator[tuple[dict[str, str], DatabaseIdentity]]:
        yield {}, {"id": "fake-postgres", "host_port": 15432}

    return UpgradeHost(
        assert_idle=lambda minimum: {"mem_available_kib": minimum},
        postgres=lease,
        container_absent=lambda _identity: postgres_removed,
    )


def _plan(output: Path, repo: Path, head: str, command: str) -> UpgradePlan:
    return UpgradePlan(
        head=head,
        tree=_git(repo, "rev-parse", "HEAD^{tree}"),
        output=str(output),
        argv=[sys.executable, "-B", "-c", command],
        resources={"minimum_mem_available_kib": 6291456},
        timeout_seconds=30,
    )


def test_head_changed_by_successful_child_publishes_nothing(
    tmp_path: Path, candidate: tuple[Path, str, str]
) -> None:
    repo, original, replacement = candidate
    output = tmp_path / "upgrade.txt"
    before = set(tmp_path.iterdir())
    plan = _plan(
        output,
        repo,
        original,
        "import subprocess; "
        f"subprocess.run(['git', 'switch', '--quiet', '--detach', {replacement!r}], "
        "check=True); print('migration-upgrade: PASS')",
    )
    with pytest.raises(RuntimeError, match="Upgrade source changed"):
        run(plan, repo, host=_host(), runtime_root=tmp_path)
    assert _git(repo, "rev-parse", "HEAD") == replacement
    assert not output.exists()
    assert not output.with_suffix(".receipt.json").exists()
    assert set(tmp_path.iterdir()) == before


def test_clean_success_publishes_raw_transcript_and_bound_receipt(
    tmp_path: Path, candidate: tuple[Path, str, str]
) -> None:
    repo, head, _ = candidate
    output = tmp_path / "upgrade.txt"
    raw = b"stdout \xff\nstderr \x80\nmigration-upgrade: PASS\n"
    argv = (
        "import os; os.write(1, b'stdout \\xff\\n'); "
        "os.write(2, b'stderr \\x80\\n'); os.write(1, b'migration-upgrade: PASS\\n')"
    )
    plan = _plan(output, repo, head, argv)
    assert run(plan, repo, host=_host(), runtime_root=tmp_path) == 0
    assert output.read_bytes() == raw + b"\nexit_code=0\n"
    receipt = json.loads(output.with_suffix(".receipt.json").read_bytes())
    assert receipt["head"] == head
    assert receipt["exit_code"] == 0
    assert receipt["stdout_stderr_bytes"] == len(raw)
    assert receipt["output_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert receipt["cleanup"] == {"postgres_removed": True, "runtime_removed": True}


def test_child_failure_is_not_published_even_with_pass_marker(
    tmp_path: Path, candidate: tuple[Path, str, str]
) -> None:
    repo, head, _ = candidate
    before = set(tmp_path.iterdir())
    plan = _plan(
        tmp_path / "upgrade.txt",
        repo,
        head,
        "print('migration-upgrade: PASS'); raise SystemExit(19)",
    )
    assert run(plan, repo, host=_host(), runtime_root=tmp_path) == 19
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("command", "postgres_removed", "reason"),
    [
        ("print('not a PASS marker')", True, "did not emit its PASS marker"),
        ("print('migration-upgrade: PASS')", False, "Incomplete T17 cleanup"),
    ],
)
def test_invalid_success_is_not_published(
    tmp_path: Path,
    candidate: tuple[Path, str, str],
    command: str,
    postgres_removed: bool,
    reason: str,
) -> None:
    repo, head, _ = candidate
    before = set(tmp_path.iterdir())
    plan = _plan(tmp_path / "upgrade.txt", repo, head, command)
    with pytest.raises(RuntimeError, match=reason):
        run(
            plan,
            repo,
            host=_host(postgres_removed=postgres_removed),
            runtime_root=tmp_path,
        )
    assert set(tmp_path.iterdir()) == before


def test_failed_transcript_rename_rolls_back_only_our_receipt(
    tmp_path: Path,
    candidate: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, head, _ = candidate
    output = tmp_path / "upgrade.txt"
    prior = set(tmp_path.iterdir())
    plan = _plan(output, repo, head, "print('migration-upgrade: PASS')")

    def concurrent_writer(source: Path, destination: Path) -> None:
        if destination == output:
            output.write_bytes(b"foreign evidence")
        move_no_replace(source, destination)

    monkeypatch.setattr(t17_upgrade, "move_no_replace", concurrent_writer)
    with pytest.raises(FileExistsError):
        run(plan, repo, host=_host(), runtime_root=tmp_path)
    assert output.read_bytes() == b"foreign evidence"
    assert set(tmp_path.iterdir()) == prior | {output}
