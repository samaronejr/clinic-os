"""The kernel model recycles an ID deterministically; no OS signal is issued."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from ops.testing.final_e2e_supervisor import require_subreaper

from infra import posture_trace_worker as worker


def test_recycled_group_never_receives_an_owned_cleanup_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released, reused = Event(), Event()
    foreign_signals: list[int] = []

    class Leader:
        pid = 41
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            released.set()
            return 0

    class Kernel:
        @staticmethod
        def killpg(pgid: int, sig: int) -> None:
            assert pgid == Leader.pid
            assert reused.is_set()
            if sig:
                foreign_signals.append(sig)
            else:
                # At this late liveness check the model's foreign job has exited.
                raise ProcessLookupError

        @staticmethod
        def waitpid(pid: int, options: int) -> tuple[int, int]:
            raise ChildProcessError

    def allocate_foreign() -> None:
        assert released.wait(timeout=3)
        reused.set()

    monkeypatch.setattr(worker, "os", Kernel)
    leader = Leader()
    with ThreadPoolExecutor(max_workers=1) as pool:
        allocation = pool.submit(allocate_foreign)
        leader.wait()
        assert reused.wait(timeout=3)
        worker._reap_group(cast("subprocess.Popen[bytes]", leader))
        allocation.result(timeout=3)
    assert foreign_signals == []


def _real_orphan() -> None:
    require_subreaper()
    # Only the nested, dedicated worker adopts children. Write readiness once;
    # these two inert processes remain in its private session.
    with subprocess.Popen(
        (sys.executable, "-m", "infra.test_posture_group_identity", "owned-fixture"),
        stdout=subprocess.PIPE,
        start_new_session=True,
    ) as leader:
        assert leader.stdout is not None
        try:
            descendant = int(leader.stdout.readline())
            assert worker._observe_exit(leader.pid, time.monotonic() + 5)
            observed = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)
            assert observed is not None
            assert observed.si_status == 0
            assert leader.returncode is None
            fd = os.pidfd_open(descendant)
            try:
                waiter = select.poll()
                waiter.register(fd, select.POLLIN)
                assert not waiter.poll(0)
                worker._reap_group(leader)
                assert waiter.poll(0)
            finally:
                os.close(fd)
        finally:
            worker._reap_group(leader)
        assert leader.returncode == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


def test_successful_leader_with_surviving_owned_child_is_reaped() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        (sys.executable, "-m", "infra.test_posture_group_identity"),
        cwd=root,
        env=os.environ
        | {"PYTHONPATH": os.pathsep.join(map(str, (root / "tests", root)))},
        timeout=12,
        check=True,
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["owned-fixture"]:
        if os.fork() == 0:
            sys.stdout.write(f"{os.getpid()}\n")
            sys.stdout.flush()
            signal.pause()
    else:
        _real_orphan()
