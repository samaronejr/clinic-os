from __future__ import annotations

import stat
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

import pytest
from ops.testing import restore_rehearsal_acceptance as restore
from ops.testing.isolation_common import IsolationError, canonical_bytes


def _attempt_ledger(tmp_path: Path, run_name: str) -> Path:
    attempt = tmp_path / run_name
    attempt.mkdir(mode=0o700)
    ledger = tmp_path / ".omo/evidence/isolation-ledger-phase1a.json"
    ledger.parent.mkdir(parents=True)
    _ = ledger.write_bytes(canonical_bytes({"attempt_root": str(attempt)}))
    return attempt


def _inert_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    def inert_lease(*_arguments: object) -> nullcontext[None]:
        return nullcontext()

    def inert_database_step(*_arguments: object) -> None:
        return

    monkeypatch.setattr(restore, "UV", Path(sys.executable))
    monkeypatch.setattr(restore, "materializer_lease", inert_lease)
    monkeypatch.setattr(restore, "public_ca_export", inert_lease)
    monkeypatch.setattr(restore, "ci_database_lease", inert_lease)
    monkeypatch.setattr(restore, "_bootstrap_database", inert_database_step)
    monkeypatch.setattr(restore, "_migrate", inert_database_step)
    monkeypatch.setattr(restore, "_seed", inert_database_step)


@pytest.mark.parametrize("run_name", ["one", "two"])
def test_restore_uses_private_disposable_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_name: str,
) -> None:
    # Given: a real attempt root, inert external leases, and a poisoned tempfile.
    attempt = _attempt_ledger(tmp_path, run_name)
    run_root = attempt / "runtime"
    monkeypatch.setattr(restore, "PROJECT_ROOT", tmp_path)
    _inert_leases(monkeypatch)

    def restore_boundary(*arguments: object) -> bytes:
        work = arguments[2]
        assert isinstance(work, Path)
        assert work.parent == run_root
        assert work.name.startswith("restore-")
        assert stat.S_IMODE(run_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(work.stat().st_mode) == 0o700
        _ = (work / "synthetic-output").write_bytes(b"temporary")
        return b"synthetic evidence\n"

    def poison_tempfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("legacy tempfile staging is forbidden")

    monkeypatch.setattr(restore, "_run_restore", restore_boundary)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", poison_tempfile)
    # When: the real restore entrypoint runs through its filesystem boundary.
    result = restore.main()
    # Then: temporary output is removed and evidence bytes reach stdout unchanged.
    assert result == 0
    assert capsys.readouterr().out == "synthetic evidence\n"
    assert list(run_root.iterdir()) == []


def test_restore_rejects_non_private_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the restore run root exists with a non-private access mode.
    attempt = _attempt_ledger(tmp_path, "attempt")
    run_root = attempt / "runtime"
    run_root.mkdir(mode=0o755)
    run_root.chmod(0o755)  # umask-explicit: the drifted mode must hold under 077
    monkeypatch.setattr(restore, "PROJECT_ROOT", tmp_path)
    _inert_leases(monkeypatch)
    # When / Then: the path policy fails closed before any rehearsal step.
    with pytest.raises(IsolationError):
        restore.main()
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o755
