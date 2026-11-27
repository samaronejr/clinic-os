"""Native, private Linux process identities across exec and partial launch."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from ops.testing.final_e2e_supervisor import require_subreaper

from infra import posture_child_lifetime as custody


def _native_assertions(prior: signal.Handlers) -> None:
    assert signal.getsignal(signal.SIGCHLD) == prior
    with custody.waitable_children() as inherited:
        assert inherited == ("ignored" if prior == signal.SIG_IGN else "default")
        assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
        pid = os.posix_spawn(
            sys.executable, (sys.executable, "-I", "-S", "-c", "pass"), os.environ
        )
        with custody.direct_child(pid) as fd:
            assert custody.exited(fd, time.monotonic() + 3)
            observed = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOWAIT)
            assert observed is not None
            assert observed.si_pid == pid
            assert custody.finish(fd, time.monotonic() + 3) == 0
    assert signal.getsignal(signal.SIGCHLD) == prior


def test_native_interrupted_constructor_recovers_escrowed_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lose the Python return value after a real private child exists. Only its
    # socket receipt is available to the production launcher. Never admit exec.
    prior = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    real_spawn = subprocess.Popen
    started: list[subprocess.Popen[bytes]] = []
    try:

        def interrupted(*_args: object, **kwargs: object) -> None:
            assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
            fds = kwargs["pass_fds"]
            assert isinstance(fds, tuple)
            env = kwargs["env"]
            assert isinstance(env, dict)
            proc = real_spawn(
                (sys.executable, "-I", "-S", "tests/infra/posture_spawn_gate.py"),
                cwd=Path(__file__).resolve().parents[2],
                pass_fds=fds,
                env=env,
                start_new_session=True,
            )
            started.append(proc)
            message = "interrupted native receipt publication"
            raise OSError(message)

        monkeypatch.setattr(subprocess, "Popen", interrupted)
        with (
            pytest.raises(OSError, match="interrupted native receipt publication"),
            custody.owned_spawn("worker", time.monotonic() + 3),
        ):
            pytest.fail("a partially constructed launcher admitted its workload")
        assert signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN
        (proc,) = started
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        proc.wait()  # Reconcile the test-only retained Popen, never signal by PID.
    finally:
        signal.signal(signal.SIGCHLD, prior)


@pytest.mark.parametrize("prior", ["default", "ignore"])
def test_native_child_remains_waitable_and_disposition_is_restored(prior: str) -> None:
    root = Path(__file__).resolve().parents[2]
    disposition = {"default": signal.SIG_DFL, "ignore": signal.SIG_IGN}[prior]
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join(map(str, (root / "tests", root))),
        "CLINIC_TEST_SIGCHLD": prior,
    }
    with custody.waitable_children(), ExitStack() as stack:
        rd, wr = os.pipe()
        stack.callback(os.close, rd)
        stack.callback(os.close, wr)
        # Only this inert external test driver deliberately supplies SIG_IGN.
        # Its exec'd child cannot exit until our own wait policy is restored.
        signal.signal(signal.SIGCHLD, disposition)
        try:
            pid = os.posix_spawn(
                sys.executable,
                (sys.executable, "-m", "infra.test_posture_signal_native"),
                env,
                file_actions=[(os.POSIX_SPAWN_DUP2, rd, 0)],
            )
        finally:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        with custody.direct_child(pid) as fd:
            try:
                assert os.write(wr, b"G") == 1
                assert custody.exited(fd, time.monotonic() + 8)
            finally:
                status = custody.finish(fd, time.monotonic() + 3)
            assert status == 0


def _session_error() -> None:
    # Only this separate driver adopts children. Both processes are inert, in
    # its own freshly created session. The child publishes readiness by pipe.
    require_subreaper()
    with (
        custody.waitable_children(),
        subprocess.Popen(
            (sys.executable, "-m", "infra.test_posture_signal_native", "family"),
            stdout=subprocess.PIPE,
            start_new_session=True,
        ) as proc,
        custody.direct_child(proc.pid) as leader_fd,
    ):
        assert proc.stdout is not None
        try:
            child_fd = os.pidfd_open(int(proc.stdout.readline()))
            try:
                assert not custody.exited(child_fd, time.monotonic())
                custody.reap_owned(proc, leader_fd, session=True)
                # The group handle, not an individual-child fallback, killed it.
                assert custody.exited(child_fd, time.monotonic() + 3)
            finally:
                os.close(child_fd)
        finally:
            if proc.returncode is None:
                custody.reap_owned(proc, leader_fd, session=True)
            custody.drain_adopted(time.monotonic() + 3)
        assert proc.returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


def test_outer_watchdog_handle_terminates_its_owned_session() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        (sys.executable, "-m", "infra.test_posture_signal_native", "session"),
        cwd=root,
        env=os.environ
        | {"PYTHONPATH": os.pathsep.join(map(str, (root / "tests", root)))},
        check=True,
    )


if __name__ == "__main__" and sys.argv[1:] == ["family"]:
    if os.fork() == 0:
        sys.stdout.write(f"{os.getpid()}\n")
        sys.stdout.flush()
    signal.pause()
elif __name__ == "__main__" and sys.argv[1:] == ["session"]:
    _session_error()
elif __name__ == "__main__":
    assert os.read(0, 1) == b"G"
    _native_assertions(
        {"default": signal.SIG_DFL, "ignore": signal.SIG_IGN}[
            os.environ["CLINIC_TEST_SIGCHLD"]
        ]
    )
