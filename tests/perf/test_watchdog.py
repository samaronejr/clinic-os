"""Native timeout evidence and deadline coverage for the public load runner."""

from __future__ import annotations

import faulthandler
import io
import multiprocessing
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from ops.perf import dataset, run, watchdog
from ops.perf.profiles import profile_named
from ops.perf.run import main

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fails", [False, True])
def test_stage_arms_native_exit_and_cancels_on_return_or_error(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    arm, cancel = Mock(), Mock()
    monkeypatch.setattr(faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", cancel)
    stream = io.StringIO()
    guard = watchdog.StageWatchdog(stream, seconds=17)
    if fails:
        message = "synthetic failure"
        with pytest.raises(RuntimeError, match=message), guard.stage("seed"):
            raise RuntimeError(message)
    else:
        with guard.stage("seed"):
            pass
    arm.assert_called_once_with(17, file=stream, exit=True)
    cancel.assert_called_once_with()
    assert "seed started deadline=17s" in stream.getvalue()
    assert f"seed {'failed' if fails else 'finished'}" in stream.getvalue()


def test_setup_and_cleanup_have_separate_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        faulthandler,
        "dump_traceback_later",
        lambda *_args, **_kwargs: events.append("armed"),
    )
    monkeypatch.setattr(
        faulthandler,
        "cancel_dump_traceback_later",
        lambda: events.append("cancelled"),
    )

    @contextmanager
    def resource() -> Iterator[int]:
        events.append("setup")
        try:
            yield 71
        finally:
            events.append("cleanup")

    stream = io.StringIO()
    message = "synthetic failure"

    def fail_with_resource() -> None:
        with watchdog.StageWatchdog(stream).context("rig", resource()) as value:
            assert value == 71
            events.append("body")
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match=message):
        fail_with_resource()
    assert events == [
        "armed",
        "setup",
        "cancelled",
        "body",
        "armed",
        "cleanup",
        "cancelled",
    ]
    assert "rig.cleanup finished" in stream.getvalue()


@pytest.mark.parametrize("deadline", ["0", "-1", "nan", "inf", "1200"])
def test_cli_rejects_unbounded_or_invalid_deadlines(deadline: str) -> None:
    with pytest.raises(SystemExit) as failure:
        main(["--profile", "P-small", "--stage-timeout", deadline])
    assert failure.value.code == 2


def _injected_hang(evidence: Path) -> None:
    ready, blocked = threading.Event(), threading.Event()

    def blocked_worker() -> None:
        ready.set()
        blocked.wait()

    def blocked_seed(*_args: object, **_kwargs: object) -> None:
        ready.wait()
        blocked.wait()

    threading.Thread(target=blocked_worker).start()
    with (
        evidence.open("x") as stream,
        watchdog.StageWatchdog(stream, seconds=0.2).activate(),
        patch.object(dataset, "seed_dataset", blocked_seed),
        patch.object(run, "ResourceRig", nullcontext),
    ):
        run.run_profile(profile_named("P-small"))


def test_injected_profile_hang_dumps_all_threads_and_exits(tmp_path: Path) -> None:
    evidence = tmp_path / "injected-hang.log"
    process = multiprocessing.get_context("spawn").Process(
        target=_injected_hang, args=(evidence,)
    )
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("stage watchdog did not stop the injected hang")
    assert process.exitcode == 1
    stacks = evidence.read_text()
    assert "seed_dataset started deadline=0.2s" in stacks
    assert "Timeout" in stacks
    assert "blocked_worker" in stacks
    assert "blocked_seed" in stacks
    assert "run_profile" in stacks
    assert "seed_dataset finished" not in stacks
