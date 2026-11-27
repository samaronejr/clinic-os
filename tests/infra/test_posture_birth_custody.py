"""Interrupt after native creation and before any usable child-generated receipt."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Never

import pytest
from ops.testing import pidfd_spawn
from ops.testing.pidfd_command import ready

from infra import posture_child_lifetime as custody
from infra import test_posture_group_identity as orphan_driver

if TYPE_CHECKING:
    import socket

    from ops.testing.pidfd_command import Command


class _InterruptedBirth:
    """Independent native wait witness; it never lends its descriptor to cleanup."""

    def __init__(self, stack: ExitStack, variant: str) -> None:
        self.variant = variant
        self.rd, self.wr = os.pipe()
        stack.callback(os.close, self.rd)
        stack.callback(os.close, self.wr)
        self.witness: int | None = None
        self.legacy: subprocess.Popen[bytes] | None = None
        self.popen = subprocess.Popen
        self.acquire = pidfd_spawn.acquire
        self.signals = 0
        self.foreign = 0
        self.send = signal.pidfd_send_signal

    @property
    def argv(self) -> tuple[str, ...]:
        # The isolated helper imports only the standard library.
        return (
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).with_name("posture_no_receipt_child.py")),
            self.variant,
        )

    def interrupt(self) -> Never:
        assert os.read(self.rd, 1) == b"R"
        assert self.witness is not None
        if self.variant == "silent-live":
            assert not ready(self.witness, time.monotonic())
        else:
            # Child already exited; waitability, not timing, determines the test.
            assert ready(self.witness, time.monotonic() + 3)
        message = "injected interrupt after kernel creation"
        raise InterruptedError(message)

    def native_birth(
        self, slot: pidfd_spawn.PidfdSlot, plan: pidfd_spawn.Launch
    ) -> None:
        fds = tuple(
            (self.wr if target == 1 else src, target) for src, target in plan.fds
        )
        self.acquire(slot, replace(plan, argv=self.argv, fds=fds))
        self.witness = os.dup(slot.value)
        self.interrupt()

    def legacy_birth(self, *_args: object, **kwargs: object) -> Never:
        env, fds = kwargs["env"], kwargs["pass_fds"]
        assert isinstance(env, dict)
        assert isinstance(fds, tuple)
        self.legacy = self.popen(
            self.argv, env=env, pass_fds=fds, stdout=self.wr, start_new_session=True
        )
        self.witness = os.pidfd_open(self.legacy.pid)
        self.interrupt()

    def by_handle(self, fd: int, sig: int, info: None, flags: int) -> None:
        assert self.witness is not None
        if not os.path.samestat(os.fstat(fd), os.fstat(self.witness)):
            self.foreign += 1
            pytest.fail("cleanup addressed a foreign pidfd")
        self.signals += 1
        self.send(fd, sig, info, flags)

    def by_number(self, _pid: int, _sig: int) -> Never:
        # A numeric operation is never dispatched to the OS.
        self.foreign += 1
        pytest.fail("cleanup attempted a bare numeric signal")

    def was_reaped(self) -> bool:
        assert self.witness is not None
        try:
            os.waitid(os.P_PIDFD, self.witness, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        return False

    def release_witness(self) -> None:
        if self.witness is not None:
            # Capture the verdict first; on the rejected implementation only,
            # the harness then reaps the already-dead child by its own handle.
            try:
                with suppress(ChildProcessError):
                    if not ready(self.witness, time.monotonic()):
                        self.send(self.witness, signal.SIGKILL, None, 0)
                    assert ready(self.witness, time.monotonic() + 3)
                    os.waitid(os.P_PIDFD, self.witness, os.WEXITED | os.WNOHANG)
            finally:
                os.close(self.witness)
        if self.legacy is not None:
            self.legacy.wait()


@pytest.mark.parametrize("variant", ["eof", "malformed", "unreadable", "silent-live"])
def test_interrupted_outer_without_usable_receipt_is_already_reaped(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, variant: str
) -> None:
    prior = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        with ExitStack() as stack:
            witness = _InterruptedBirth(stack, variant)
            stack.callback(witness.release_witness)
            monkeypatch.setattr(subprocess, "Popen", witness.legacy_birth)
            monkeypatch.setattr(pidfd_spawn, "acquire", witness.native_birth)
            monkeypatch.setattr(signal, "pidfd_send_signal", witness.by_handle)
            monkeypatch.setattr(os, "kill", witness.by_number)
            monkeypatch.setattr(os, "killpg", witness.by_number)
            if variant == "unreadable":

                def unreadable(_channel: socket.socket) -> Never:
                    message = "injected unreadable ownership receipt"
                    raise OSError(message)

                monkeypatch.setattr(custody, "_receive", unreadable)
            with (
                pytest.raises(InterruptedError, match="after kernel creation"),
                custody.owned_spawn("worker", time.monotonic() + 3),
            ):
                pytest.fail("interrupted constructor admitted a workload")
            already_reaped = witness.was_reaped()
            request.node.user_properties.append(
                (
                    "birth-custody",
                    {
                        "variant": variant,
                        "already_reaped": already_reaped,
                        "foreign_signals": witness.foreign,
                        "owned_pidfd_signals": witness.signals,
                    },
                )
            )
            assert already_reaped
            assert witness.foreign == 0
            assert witness.signals == 1
    finally:
        signal.signal(signal.SIGCHLD, prior)


def test_orphan_driver_timeout_uses_handle_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numeric: list[float] = []
    stable: list[float] = []

    def legacy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if isinstance(timeout, (int, float)):
            numeric.append(timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def owned(
        argv: list[str], options: Command, *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check
        stable.append(options.timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", legacy)
    monkeypatch.setattr(orphan_driver, "run_text", owned, raising=False)
    orphan_driver.test_successful_leader_with_surviving_owned_child_is_reaped()
    assert numeric == []
    assert stable == [12]
