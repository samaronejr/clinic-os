from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from ops.testing import ci_postgres_controller as controller

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture
def supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, str | int | bool], None],
) -> Iterator[Path]:
    # Given: real daemon execution with synthetic lease boundaries only.
    bootstrap = tmp_path / "fixture-python"
    bootstrap.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "import os\n"
        "import signal\n"
        f"sys.path.insert(0, {str(controller.PROJECT_ROOT)!r})\n"
        "from contextlib import contextmanager\n"
        "from pathlib import Path\n"
        "from ops.testing import ci_postgres_daemon as daemon\n"
        "root = Path(sys.argv[-1])\n"
        "(root.parent / 'fixture-pid').write_text(str(os.getpid()))\n"
        "mode = (root.parent / 'mode').read_text()\n"
        "if mode == 'prestart':\n"
        "    raise RuntimeError('fixture_startup_failure')\n"
        "@contextmanager\n"
        "def lease(*args):\n"
        "    yield None\n"
        "@contextmanager\n"
        "def database(*args):\n"
        "    if mode == 'startup':\n"
        "        raise RuntimeError('fixture_startup_failure')\n"
        "    if mode == 'hung':\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        print('fixture_hung_startup', flush=True)\n"
        "        signal.pause()\n"
        "    yield None\n"
        "    if mode == 'teardown':\n"
        "        raise RuntimeError('fixture_teardown_failure')\n"
        "daemon.materializer_lease = lease\n"
        "daemon.public_ca_export = lease\n"
        "daemon.ci_database_lease = database\n"
        "daemon.write_environment = lambda *args: None\n"
        "sys.argv = [sys.argv[0], str(root)]\n"
        "raise SystemExit(daemon.main())\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(bootstrap))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(controller.PROJECT_ROOT / ".venv"))
    sentinel = tmp_path / "foreign-sentinel"
    sentinel.write_bytes(b"foreign-resource-unchanged")
    yield tmp_path
    assert sentinel.read_bytes() == b"foreign-resource-unchanged"
    pid = int((tmp_path / "fixture-pid").read_text())
    assert controller._start(pid) == -1
    shutil.rmtree(tmp_path)
    record_property("fixture_pid", pid)
    record_property("temp_resource", str(tmp_path))
    record_property("child_reaped", controller._start(pid) == -1)
    record_property("temp_removed", not tmp_path.exists())


def _stop_daemon(root: Path) -> int:
    """Reap the owned child concurrently, as the external workflow parent does."""
    record = json.loads((root / "supervisor.json").read_text())
    pid = int(record["pid"])
    with ThreadPoolExecutor(max_workers=1) as executor:
        completion = executor.submit(os.waitpid, pid, 0)
        try:
            controller._down()
        finally:
            if not completion.done():
                os.killpg(pid, signal.SIGTERM)
            _, status = completion.result(timeout=10)
    return os.waitstatus_to_exitcode(status)


def test_normal_supervisor_lifecycle_cleans_owned_state(supervisor: Path) -> None:
    # Given: successful synthetic database lease startup and teardown.
    (supervisor / "mode").write_text("normal")
    controller._up()
    root = supervisor / controller.STATE_NAME
    assert (root / "ready").is_file()

    # When: stop traverses the real signal and context-manager teardown path.
    status = _stop_daemon(root)

    # Then: success removes only owned supervisor state.
    assert status == 0
    assert not root.exists()


@pytest.mark.parametrize("mode", ["startup", "prestart"])
def test_startup_failure_retains_private_diagnostic(
    supervisor: Path,
    mode: str,
) -> None:
    # Given: a child that throws at the database startup boundary.
    (supervisor / "mode").write_text(mode)

    # When: the real controller observes the failed child.
    with pytest.raises(RuntimeError):
        controller._up()

    # Then: the diagnosable cause survives in private owner-bound state.
    root = supervisor / controller.STATE_NAME
    diagnostic = root / "daemon.log"
    assert diagnostic.is_file()
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert diagnostic.stat().st_uid == os.geteuid()
    assert root.stat().st_mode & 0o777 == 0o700
    assert "fixture_startup_failure" in diagnostic.read_text()
    assert not (root / "cleaned").exists()


def test_teardown_failure_retains_state_without_success(supervisor: Path) -> None:
    # Given: startup succeeds but the database lease teardown throws.
    (supervisor / "mode").write_text("teardown")
    controller._up()
    root = supervisor / controller.STATE_NAME

    # When: down requests teardown and the child fails.
    with pytest.raises(RuntimeError, match="did not prove cleanup"):
        _stop_daemon(root)

    # Then: cleanup cannot discard state or attest success.
    assert not (root / "cleaned").exists()
    assert (root / "supervisor.json").is_file()
    assert "fixture_teardown_failure" in (root / "daemon.log").read_text()


def test_hung_startup_is_bounded_and_retains_unproved_state(
    supervisor: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a lease that never enters and ignores termination.
    (supervisor / "mode").write_text("hung")
    monkeypatch.setattr(controller, "READY_BOUND_SECONDS", 1)
    monkeypatch.setattr(controller, "CLEANUP_BOUND_SECONDS", 1)

    # When: the existing bounded controller escalates and reaps its child.
    with pytest.raises(RuntimeError):
        controller._up()

    # Then: failure leaves private diagnosis and no cleanup attestation.
    root = supervisor / controller.STATE_NAME
    assert "fixture_hung_startup" in (root / "daemon.log").read_text()
    assert not (root / "cleaned").exists()


def test_reused_pid_refuses_to_signal_foreign_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: stale identity points at this live process with different start ticks.
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    root = tmp_path / controller.STATE_NAME
    root.mkdir(mode=0o700)
    controller.write_record(
        root / "supervisor.json",
        {
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "start_ticks": controller._start(os.getpid()) + 1,
        },
    )
    sentinel = root / "daemon.log"
    sentinel.write_bytes(b"foreign-sentinel")

    # When: down authenticates the recorded process before signaling.
    with pytest.raises(RuntimeError, match="cannot be authenticated"):
        controller._down()

    # Then: stale state and the foreign process remain untouched.
    assert sentinel.read_bytes() == b"foreign-sentinel"
    assert (root / "supervisor.json").is_file()
