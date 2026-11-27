"""Deterministic lost-identity and constructor-fault controls; no foreign OS signals."""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from threading import Event
from types import SimpleNamespace
from typing import Self, cast

import psycopg
import pytest

from infra import posture_child_lifetime as custody
from infra import posture_observation as observation
from infra import posture_trace_worker as worker


class _Leader:
    pid = 41
    returncode: int | None = None
    stdout = None

    def __init__(self) -> None:
        self.stderr = io.BytesIO()

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.wait()


class _ReuseKernel:
    """Private sockets carry an inert file description in place of a real pidfd."""

    def __init__(self, stack: ExitStack, *, partial_spawn: bool) -> None:
        self.partial = partial_spawn
        self.released, self.reused = Event(), Event()
        self.foreign: list[int] = []
        self.handles: list[int] = []
        self.dispositions: list[object] = []
        self.terminated = False
        self.reaped = False
        self.leader = _Leader()
        self.stack = stack
        rd, wr = os.pipe()
        stack.callback(os.close, rd)
        stack.callback(os.close, wr)
        self.token = rd
        self.inode = os.fstat(rd).st_ino

    def reuse(self) -> None:
        assert self.released.wait(timeout=3)
        self.reused.set()

    def retire(self) -> None:
        self.released.set()
        assert self.reused.wait(timeout=3)

    def spawn(
        self, *_a: object, pass_fds: tuple[int, ...] = (), **_kw: object
    ) -> _Leader:
        self.dispositions.append(signal.getsignal(signal.SIGCHLD))
        if pass_fds:
            channel = self.stack.enter_context(
                socket.socket(fileno=os.dup(pass_fds[0]))
            )
            assert socket.send_fds(channel, [b"P"], [self.token]) == 1
        if self.partial:
            message = "fixture constructor interrupted after child creation"
            raise OSError(message)
        return self.leader

    def observation_error(self, _id: int, _until: float) -> bool:
        # Stress both dispositions with a number already reassigned. Under
        # DFL this models loss to a second waiter, not spontaneous auto-reap.
        self.retire()
        message = "fixture observation failed after identity loss"
        raise OSError(message)

    def by_number(self, pgid: int, sig: int) -> None:
        assert pgid == self.leader.pid
        if self.reused.is_set() and sig:
            self.foreign.append(sig)

    def by_handle(self, fd: int, sig: int, _info: object, _flags: int) -> None:
        assert os.fstat(fd).st_ino == self.inode
        self.handles.append(sig)
        if self.reused.is_set():
            raise ProcessLookupError
        self.terminated = True

    def waitid(self, kind: int, fd: int, flags: int) -> SimpleNamespace | None:
        if kind == os.P_ALL:
            raise ChildProcessError
        assert kind == os.P_PIDFD
        assert os.fstat(fd).st_ino == self.inode
        if flags & os.WNOWAIT:
            return None
        if not self.terminated:
            message = "fixture wait status already released"
            raise ChildProcessError(message)
        self.reaped = True
        self.retire()
        return SimpleNamespace(si_status=signal.SIGKILL, si_code=os.CLD_KILLED)

    @staticmethod
    def legacy_wait(_pid: int, _flags: int) -> tuple[int, int]:
        raise ChildProcessError


@pytest.mark.parametrize(
    ("disposition", "partial"),
    [(signal.SIG_IGN, False), (signal.SIG_DFL, False), (signal.SIG_IGN, True)],
    ids=["inherited-ignore", "inherited-default", "partial-spawn"],
)
def test_lost_number_cannot_authorize_error_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    disposition: signal.Handlers,
    partial: bool,
) -> None:
    saved = signal.signal(signal.SIGCHLD, disposition)
    try:
        with ExitStack() as stack, ThreadPoolExecutor(max_workers=1) as pool:
            kernel = _ReuseKernel(stack, partial_spawn=partial)
            recycled = pool.submit(kernel.reuse)
            monkeypatch.setenv("TEST_SUPERUSER_DATABASE_URL", "unused-in-model")
            monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: nullcontext(None))
            monkeypatch.setattr(worker, "require_subreaper", lambda: None)
            monkeypatch.setattr(worker, "activity", lambda _: [])
            monkeypatch.setattr(worker, "_watch", lambda _: Future())
            monkeypatch.setattr(
                worker, "_stage_reader", lambda *_a: Future(), raising=False
            )
            # Both implementations meet the same kernel model at their launch,
            # observation and signal interfaces. No real syscall can signal anyone.
            monkeypatch.setattr(subprocess, "Popen", kernel.spawn)
            monkeypatch.setattr(os, "killpg", kernel.by_number)
            monkeypatch.setattr(os, "kill", kernel.by_number)
            monkeypatch.setattr(os, "waitid", kernel.waitid)
            monkeypatch.setattr(os, "waitpid", kernel.legacy_wait)
            monkeypatch.setattr(signal, "pidfd_send_signal", kernel.by_handle)
            monkeypatch.setattr(
                worker, "_observe_exit", kernel.observation_error, raising=False
            )
            monkeypatch.setattr(
                worker, "exited", kernel.observation_error, raising=False
            )
            monkeypatch.setattr(custody, "exited", lambda _fd, _until: True)
            try:
                with pytest.raises(
                    OSError,
                    match=(
                        r"fixture (constructor interrupted|observation failed|"
                        r"wait status already released)"
                    ),
                ):
                    worker.main()
            finally:
                # Also discharge the simulated allocator on a legacy constructor
                # error that never took ownership of its already-created child.
                kernel.retire()
                recycled.result(timeout=3)
            request.node.user_properties.append(
                (
                    "posture-custody",
                    {
                        "inherited": "ignored"
                        if disposition == signal.SIG_IGN
                        else "default",
                        "default_at_spawn": kernel.dispositions == [signal.SIG_DFL],
                        "restored": signal.getsignal(signal.SIGCHLD) == disposition,
                        "foreign_signals": len(kernel.foreign),
                        "pidfd_signals": len(kernel.handles),
                        "partial_spawn": partial,
                        "partial_child_reaped": kernel.reaped,
                    },
                )
            )
            assert kernel.foreign == []
            if partial:
                assert kernel.reaped
            assert kernel.handles == [signal.SIGKILL]
            assert kernel.dispositions == [signal.SIG_DFL]
            assert signal.getsignal(signal.SIGCHLD) == disposition
    finally:
        signal.signal(signal.SIGCHLD, saved)


def test_closing_process_window_precedes_late_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted, resume = Event(), Event()
    reads: list[int] = []

    def snapshot(fd: int) -> list[dict[str, object]]:
        reads.append(fd)
        return []

    monkeypatch.setattr(observation, "process_group", snapshot)
    window = observation.ProcessWindow(41)

    def late_sample() -> list[dict[str, object]]:
        admitted.set()
        assert resume.wait(timeout=3)
        return window.snapshot()

    with ThreadPoolExecutor(max_workers=1) as pool:
        sample = pool.submit(late_sample)
        assert admitted.wait(timeout=3)
        window.close()
        resume.set()
        assert sample.result(timeout=3) == []
    assert reads == []


def test_inflight_proc_read_finishes_before_release_not_database_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reading, allow_read, closing = Event(), Event(), Event()
    db_started, allow_db, closed, stop = Event(), Event(), Event(), Event()
    order: list[str] = []

    def read_proc(_fd: int) -> list[dict[str, object]]:
        reading.set()
        assert allow_read.wait(timeout=3)
        order.append("proc-read-complete")
        return []

    def read_db(_db: object) -> list[dict[str, object]]:
        db_started.set()
        assert allow_db.wait(timeout=3)
        return []

    monkeypatch.setattr(observation, "process_group", read_proc)
    monkeypatch.setattr(worker, "activity", read_db)
    window = observation.ProcessWindow(41)

    def release() -> None:
        closing.set()
        window.close()
        order.append("released")
        closed.set()

    records: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(
            worker._sample, cast("psycopg.Connection", None), window, stop, records
        )
        assert reading.wait(timeout=3)
        cleanup = pool.submit(release)
        try:
            assert closing.wait(timeout=3)
            allow_read.set()
            assert db_started.wait(timeout=3)
            assert closed.wait(timeout=3)  # The DB read still has not returned.
            assert window.snapshot() == []
        finally:
            stop.set()
            allow_read.set()
            allow_db.set()
        task.result(timeout=3)
        cleanup.result(timeout=3)
    assert order == ["proc-read-complete", "released"]
    assert records
