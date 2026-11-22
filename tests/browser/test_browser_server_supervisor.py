from __future__ import annotations

import contextlib
import errno
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Any, Final, cast

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import browser_server_stages as stage
from ops.testing import browser_server_supervisor as supervisor
from ops.testing.browser_secret_channel import FRAME_COUNT, SecretChannel
from ops.testing.browser_server_flow import run_session
from ops.testing.browser_server_journal import (
    BarrierJournal,
    BrowserJournalError,
    read_records,
    resume,
)
from ops.testing.browser_totp_helpers import HelperSequence
from ops.testing.final_e2e_supervisor import require_subreaper

from browser.browser_server_fakes import RecordingEffects

TOKEN: Final = "9f2c0f1e5b7a4d3c8e6f1a2b3c4d5e6f"  # noqa: S105 - synthetic session token, not a credential.
REVERSE_TEARDOWN: Final = ("runner_remove", "stop_master", "release_database")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def _spawn(
    code: str, log: Path, *, pass_fds: tuple[int, ...] = ()
) -> supervisor.SupervisedMaster:
    with log.open("wb") as stream:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter, closed argv.
            [sys.executable, "-c", code],
            start_new_session=True,
            pass_fds=pass_fds,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    return supervisor.SupervisedMaster(process)


def _full_run(tmp_path: Path, name: str) -> tuple[BarrierJournal, RecordingEffects]:
    read_fd, write_fd = os.pipe()
    channel = SecretChannel(write_fd)
    helpers = HelperSequence()
    effects = RecordingEffects(channel, helpers)
    journal = BarrierJournal(tmp_path / name)
    run_session(effects, journal, helpers)
    assert len(channel.sent) == FRAME_COUNT
    channel.close()
    os.close(read_fd)
    return journal, effects


def test_request_protocol_authenticates_token_sequence_kind_and_port() -> None:
    raw = supervisor.build_request(supervisor.START_MASTER, 58419, 0, TOKEN)

    assert supervisor.parse_request(raw, TOKEN, 0) == (supervisor.START_MASTER, 58419)
    for token, sequence in ((TOKEN, 1), ("wrong-token", 0)):
        with pytest.raises(supervisor.SupervisorError, match="not authenticated"):
            supervisor.parse_request(raw, token, sequence)
    with pytest.raises(supervisor.SupervisorError, match="invalid or protected"):
        supervisor.build_request(supervisor.START_MASTER, 5432, 0, TOKEN)
    with pytest.raises(supervisor.SupervisorError, match="not a supported"):
        supervisor.build_request("exec-anything", 58419, 0, TOKEN)
    with pytest.raises(supervisor.SupervisorError, match="closed key set"):
        supervisor.parse_request(b'{"kind":"start-master"}', TOKEN, 0)


def test_response_is_the_fixed_two_worker_handoff() -> None:
    raw = supervisor.build_response(supervisor.MASTER_STARTED, 4242, 4242, 0)

    assert supervisor.parse_response(raw, supervisor.MASTER_STARTED, 0) == (4242, 4242)
    with pytest.raises(supervisor.SupervisorError, match="fixed supervisor handoff"):
        supervisor.parse_response(raw, supervisor.MASTER_STOPPED, 0)
    with pytest.raises(supervisor.SupervisorError, match="fixed supervisor handoff"):
        supervisor.parse_response(raw, supervisor.MASTER_STARTED, 1)


def test_supervised_argv_is_frozen_and_preloaded(tmp_path: Path) -> None:
    argv = supervisor.supervised_argv(
        Path(sys.executable), 58419, tmp_path / "access.log"
    )

    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "gunicorn", "--bind"]
    assert argv[4] == "fd://58419"
    assert "--workers=2" in argv
    assert "--worker-class=sync" in argv
    assert "--threads=1" in argv
    assert "--timeout=30" in argv
    assert "--preload" in argv
    assert argv[-1] == "config.wsgi:application"
    assert not any(item in {"sh", "-c"} for item in argv)
    with pytest.raises(supervisor.SupervisorError, match="executable absolute path"):
        supervisor.supervised_argv(tmp_path / "missing", 58419, tmp_path / "a.log")


def test_master_runs_in_its_own_group_and_is_reaped_on_term(tmp_path: Path) -> None:
    master = _spawn("import time; time.sleep(120)", tmp_path / "term.log")
    pid, pgid = master.pid, master.pgid

    assert pgid == pid
    assert _alive(pid)
    master.terminate()

    assert master.process.poll() is not None
    assert not _alive(pid)


def test_term_resistant_master_is_escalated_to_kill_and_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "TERMINATION_GRACE_SECONDS", 1)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as ready:
        try:
            master = _spawn(
                "import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"os.write({write_fd}, b'R'); signal.pause()",
                tmp_path / "kill.log",
                pass_fds=(write_fd,),
            )
        finally:
            os.close(write_fd)
        try:
            assert select.select([ready], [], [], 10)[0]
            assert ready.read(1) == b"R"
            pid = master.pid

            master.terminate()

            assert master.process.poll() is not None
            assert master.process.returncode == -signal.SIGKILL
            assert not _alive(pid)
        finally:
            master.terminate()


def test_terminate_is_idempotent_and_leaves_no_zombie(tmp_path: Path) -> None:
    master = _spawn("import time; time.sleep(120)", tmp_path / "idem.log")
    master.terminate()
    master.terminate()

    assert master.process.poll() is not None
    assert not _alive(master.pid)


def _rescue_worker(worker: int) -> None:
    def reap() -> None:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(worker, 0)

    waiter = Thread(target=reap, daemon=True)
    waiter.start()
    waiter.join(timeout=10)
    assert not waiter.is_alive()


@pytest.mark.parametrize("entrypoint", ["controller", "renewal"])
def test_dead_master_teardown_without_pidfd_releases_the_inherited_listener(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adopt a leaked worker in the red case so test-side rescue can reap it.
    require_subreaper()
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(supervisor, "TERMINATION_GRACE_SECONDS", 1)
    worker: int | None = None
    processes: list[subprocess.Popen[bytes]] = []
    ready, writer = socket.socketpair()
    with ready, writer, controller.reserve_listener() as reserved:
        ready.settimeout(10)
        address = reserved.getsockname()
        fds = (reserved.fileno(), writer.fileno())
        argv = [
            sys.executable,
            "-c",
            "import os, signal\n"
            "if os.fork() == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"    os.write({writer.fileno()}, str(os.getpid()).encode())\n"
            "signal.pause()\n",
        ]

        def kill_master(server: subprocess.Popen[bytes]) -> None:
            nonlocal worker
            worker = int(ready.recv(32))
            server.kill()
            assert server.wait(timeout=10) == -signal.SIGKILL

        try:
            if entrypoint == "controller":
                popen = subprocess.Popen

                def spawn(
                    _argv: list[str],
                    **kwargs: Any,  # noqa: ANN401 - forward Popen options.
                ) -> subprocess.Popen[bytes]:
                    assert kwargs["pass_fds"] == (reserved.fileno(),)
                    process = popen(argv, **{**kwargs, "pass_fds": fds})
                    processes.append(process)
                    return process

                monkeypatch.setattr(subprocess, "Popen", spawn)
                monkeypatch.setattr(controller, "reserve_listener", lambda: reserved)
                for name in controller.REQUIRED_ENVIRONMENT:
                    monkeypatch.setenv(name, "synthetic")
                monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
                monkeypatch.setattr(
                    controller, "wait_until_ready", lambda *_: kill_master(processes[0])
                )
                monkeypatch.setattr(controller, "build_suite", lambda *_: dict)
                monkeypatch.setattr(controller, "export_artifacts", lambda *_: {})
                assert controller.run_suite("patient", ["patient"]) == os.EX_OK
            else:
                with reserved:
                    master = supervisor.start_master(
                        argv,
                        {},
                        tmp_path / "server.log",
                        owner=[],
                        pass_fds=fds,
                    )
                    processes.append(master.process)
                kill_master(master.process)
                master.terminate()

            assert reserved.fileno() == -1
            with socket.socket() as probe:
                probe.bind(address)
            assert worker is not None
            assert not _alive(worker)
        finally:
            for process in processes:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            if worker is not None:
                _rescue_worker(worker)


@pytest.mark.parametrize("listening", [True, False], ids=["listener", "reservation"])
def test_release_check_requires_an_ordinary_bind(listening: bool) -> None:
    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind((controller.HOST, 0))
        address = held.getsockname()
        if listening:
            held.listen()
        with pytest.raises(supervisor.SupervisorError, match="port was not released"):
            supervisor.require_released_port(int(address[1]))
    # The post-teardown caller can bind only after the reservation closes.
    supervisor.require_released_port(int(address[1]))
    with socket.socket() as probe:
        probe.bind(address)


def test_release_check_waits_for_tcp_expiry_but_refuses_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with controller.reserve_listener() as listener, socket.socket() as client:
        address = listener.getsockname()
        client.settimeout(10)
        listener.settimeout(10)
        client.connect(address)
        accepted, _ = listener.accept()
        with accepted:
            accepted.settimeout(10)
            # The server sends the first FIN and therefore owns TIME_WAIT.
            accepted.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
            client.shutdown(socket.SHUT_WR)
            assert accepted.recv(1) == b""

    now = 100.0
    deadlines: list[float] = []

    def advance_clock(until: float) -> None:
        nonlocal now
        deadlines.append(until)
        now = until

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(supervisor, "PORT_RELEASE_SECONDS", 1)
    monkeypatch.setattr(supervisor, "_wait_for_tcp_expiry", advance_clock)
    with pytest.raises(supervisor.SupervisorError, match="port was not released"):
        supervisor.require_released_port(int(address[1]))

    assert deadlines == [101.0]


class _HandoffInterruptError(Exception):
    """Raised by the test signal handler at the spawn/handoff seam."""


def _raise_on_signal(_signum: int, _frame: object) -> None:
    raise _HandoffInterruptError


def test_start_master_reaps_the_spawn_when_the_handoff_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal after the real spawn but before the handoff cannot leak.

    ``Popen`` really executes; only the ``SupervisedMaster`` construction is
    interrupted. The spawned group must be reaped before the exception
    propagates — the caller never receives a handle to clean up. The
    assertions below observe the state the production cleanup left; the
    unconditional rescue in ``finally`` runs only afterwards, so this test
    fails if production cleanup is removed.
    """
    spawned: list[subprocess.Popen[bytes]] = []
    real_master = supervisor.SupervisedMaster

    def interrupted_handoff(process: subprocess.Popen[bytes]) -> object:
        spawned.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return real_master(process)

    monkeypatch.setattr(supervisor, "SupervisedMaster", interrupted_handoff)
    previous = signal.signal(signal.SIGTERM, _raise_on_signal)
    owner: list[supervisor.SupervisedMaster] = []
    try:
        with pytest.raises(_HandoffInterruptError):
            supervisor.start_master(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                {},
                tmp_path / "handoff.log",
                owner=owner,
            )
        # Production cleanup outcome, observed before any test-side rescue.
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        assert not _alive(spawned[0].pid)
        assert len(owner) == 1
        assert owner[0].process is spawned[0]
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_start_master_reaps_when_signaled_after_ownership_is_published(
    tmp_path: Path,
) -> None:
    """A signal inside the ownership append still reaps before propagating.

    The assertions observe the state production cleanup left; the
    unconditional rescue in ``finally`` runs only afterwards, so this test
    fails if production cleanup is removed.
    """

    class SignalOnAppend(list[supervisor.SupervisedMaster]):
        def append(self, item: supervisor.SupervisedMaster) -> None:
            super().append(item)
            os.kill(os.getpid(), signal.SIGTERM)

    owner = SignalOnAppend()
    previous = signal.signal(signal.SIGTERM, _raise_on_signal)
    try:
        with pytest.raises(_HandoffInterruptError):
            supervisor.start_master(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                {},
                tmp_path / "published.log",
                owner=owner,
            )
        # Production cleanup outcome, observed before any test-side rescue.
        assert len(owner) == 1
        assert owner[0].process.poll() is not None
        assert not _alive(owner[0].pid)
    finally:
        signal.signal(signal.SIGTERM, previous)
        for master in owner:
            if master.process.poll() is None:
                master.process.kill()
                master.process.wait(timeout=10)


def test_start_master_reaps_when_signaled_before_the_spawn_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal while the spawned child is still unowned cannot leak.

    The real ``Popen`` runs; the signal is delivered after the child exists
    but before ``start_master`` regains control — the interval that used to
    sit between the spawn and the cleanup ``try``. The assertions observe
    the state production cleanup left; the unconditional rescue in
    ``finally`` runs only afterwards, so this test fails if production
    cleanup is removed.
    """
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def spawn_then_signal(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(*cast("Any", args), **cast("Any", kwargs))
        spawned.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn_then_signal)
    previous = signal.signal(signal.SIGTERM, _raise_on_signal)
    owner: list[supervisor.SupervisedMaster] = []
    try:
        with pytest.raises(_HandoffInterruptError):
            supervisor.start_master(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                {},
                tmp_path / "spawn.log",
                owner=owner,
            )
        # Production cleanup outcome, observed before any test-side rescue.
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        assert not _alive(spawned[0].pid)
        assert len(owner) == 1
        assert owner[0].process is spawned[0]
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_start_master_without_cleanup_leaves_the_spawn_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: with cleanup disabled the spawn survives.

    This is the same spawn→publish signal seam as above with production
    ``_terminate_group`` replaced by a no-op. It proves the positive
    assertions observe production cleanup rather than test-side rescue: the
    child is still alive when ``start_master`` raises, and only the
    unconditional rescue in ``finally`` reaps it.
    """
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def spawn_then_signal(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(*cast("Any", args), **cast("Any", kwargs))
        spawned.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn_then_signal)
    monkeypatch.setattr(supervisor, "_terminate_group", lambda process: None)
    previous = signal.signal(signal.SIGTERM, _raise_on_signal)
    owner: list[supervisor.SupervisedMaster] = []
    try:
        with pytest.raises(_HandoffInterruptError):
            supervisor.start_master(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                {},
                tmp_path / "mutant.log",
                owner=owner,
            )
        # With production cleanup disabled the spawn is still alive here —
        # the exact state the positive tests assert against.
        assert len(spawned) == 1
        assert spawned[0].poll() is None
        assert _alive(spawned[0].pid)
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert spawned[0].poll() is not None
    assert not _alive(spawned[0].pid)


def test_full_session_journal_is_chained_and_replayable(tmp_path: Path) -> None:
    journal, _ = _full_run(tmp_path, "session.jsonl")

    records = read_records(journal.path)
    assert len(records) == len(stage.STAGES)
    assert [record["stage"] for record in records] == list(journal.recorded)
    assert records[0]["previous_sha256"] == "0" * 64
    for index, record in enumerate(records[1:], start=1):
        assert record["previous_sha256"] == records[index - 1]["chain_sha256"]
        assert record["sequence"] == index


def test_teardown_runs_in_reverse_dependency_order(tmp_path: Path) -> None:
    _, effects = _full_run(tmp_path, "teardown.jsonl")

    positions = [effects.calls.index(name) for name in REVERSE_TEARDOWN]
    assert positions == sorted(positions)
    assert effects.calls.index("dispatch:patient") < positions[0]


def test_journal_can_resume_at_every_boundary_but_never_when_sealed(
    tmp_path: Path,
) -> None:
    journal, _ = _full_run(tmp_path, "resume.jsonl")
    complete = journal.recorded
    with pytest.raises(BrowserJournalError, match="cannot be resumed"):
        resume(journal.path)

    for boundary in range(1, len(complete)):
        path = tmp_path / f"partial-{boundary}.jsonl"
        partial = BarrierJournal(path)
        for item in complete[:boundary]:
            partial.record(item)
        successor = resume(path)
        assert successor.recorded == complete[:boundary]
        assert not successor.sealed
        for item in complete[boundary:]:
            successor.record(item)
        assert successor.recorded == complete
        assert successor.sealed


def test_successor_cannot_skip_a_missing_predecessor(tmp_path: Path) -> None:
    path = tmp_path / "skip.jsonl"
    journal = BarrierJournal(path)
    journal.record(stage.LEDGER_REFRESHED)
    journal.record(stage.MATERIALIZER_ACTIVE)

    successor = resume(path)
    with pytest.raises(Exception, match="requires"):
        successor.record(stage.OWNER_RELEASE)
    with pytest.raises(Exception, match="requires"):
        successor.record(stage.RUNNER_ACTIVE)


def test_supervisor_module_never_shells_out() -> None:
    source = Path(supervisor.__file__).read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "os.system" not in source
    assert "/bin/sh" not in source
