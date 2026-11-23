"""Exercise the two real CLI dispatchers with deterministic signal/fault cuts."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Final, Never, cast
from unittest.mock import Mock
from urllib.parse import quote
from uuid import uuid4

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import browser_server_supervisor as supervisor
from ops.testing import realtime_stack
from ops.testing import renewal_runner as runner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ROOT: Final = Path(__file__).resolve().parents[2]
CLI_PROBE: Final = """
import contextlib, os, runpy, signal, subprocess, sys
from ops.testing import browser_server_supervisor as supervisor

module, descriptor = sys.argv[1:]
os.set_inheritable(int(descriptor), False)
sys.argv[:] = [module, *(
    ["suite", "patient"] if "controller" in module
    else [
        "browser", "--suite", "journey-first-day",
        "--artifact-root", os.environ["CLINIC_BROWSER_EVIDENCE_ROOT"],
    ]
)]
start_master = supervisor.start_master

def launch(argv, environment, log, *, owner, pass_fds):
    listener = pass_fds[0]
    read_fd, write_fd = os.pipe()
    try:
        master = start_master(
            [sys.executable, "-c",
             "import os, signal, sys; child = os.fork(); "
             "os.write(int(sys.argv[1]), str(child).encode() + b'\\\\n') "
             "if child else None; "
             "signal.pause()", str(write_fd)],
            environment, log, owner=owner, pass_fds=(listener, write_fd),
        )
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, "rb") as ready:
        worker_pid = int(ready.readline())
    port = listener.getsockname()[1]
    os.write(int(descriptor), f"{master.pid} {worker_pid} {port}".encode())
    signal.pause()
    raise AssertionError("published-master cut did not unwind")

def start_server(listener, environment, log, *, owner):
    return launch([], environment, log, owner=owner, pass_fds=(listener,))

def install(frame, event, arg):
    scope = frame.f_globals
    if event != "call" or frame.f_code.co_name != "main":
        return None
    if scope.get("__name__") != "__main__":
        return None
    spec = scope.get("__spec__")
    if spec is None or spec.name != module:
        return None
    sys.settrace(None)
    if "controller" in module:
        scope["start_server"] = start_server
    else:
        scope["start_master"] = launch
        scope["_resolve_browser"] = lambda *_: "/unused"
        scope["_capture_source"] = lambda *_: ({}, "fixture")
        scope["_require_app_role"] = lambda *_: None
        scope["realtime_broker"] = lambda *_, **__: contextlib.nullcontext()
    return None

sys.settrace(install)
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""
WAIT: Final = 10


def _collect_pid(pid: int) -> None:
    """Reap a baseline leak only after recording what the actual CLI left."""
    if not Path(f"/proc/{pid}").exists():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    # These are reparented to the test subreaper once the launcher is reaped.
    reaped: list[int] = []

    def wait() -> None:
        reaped.append(os.waitpid(pid, 0)[0])

    waiter = Thread(target=wait, daemon=True)
    waiter.start()
    waiter.join(WAIT)
    assert reaped == [pid]


def _signal_cli(
    launcher: str, signum: signal.Signals, root: Path
) -> tuple[int, bool, bool]:
    root.mkdir(mode=0o700)
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT),
        "CLINIC_BROWSER_EVIDENCE_ROOT": str(root),
        "CLINIC_BROWSER_BASE_URL": "http://127.0.0.1:1",
        "CLINIC_BROWSER_CLINIC_ID": "local",
        "CLINIC_BROWSER_USERNAME": "local",
        "CLINIC_BROWSER_PASSWORD": uuid4().hex,
        "CLINIC_RENEWAL_APP_DATABASE_URL": (
            f"postgresql://clinic_app:{uuid4().hex}@127.0.0.1:5/clinic"
        ),
    }
    read_fd, write_fd = os.pipe()
    pids: list[int] = []
    with os.fdopen(read_fd, "rb") as published:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and probe.
                [
                    sys.executable,
                    "-c",
                    CLI_PROBE,
                    f"ops.testing.{launcher}",
                    str(write_fd),
                ],
                cwd=ROOT,
                env=env,
                pass_fds=(write_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(write_fd)
        try:
            assert select.select([published], [], [], WAIT)[0], "CLI did not publish"
            master, worker, port = map(int, os.read(published.fileno(), 256).split())
            pids.extend((master, worker))
            assert all(Path(f"/proc/{pid}").exists() for pid in pids)
            os.kill(process.pid, signum)
            code = process.wait(timeout=WAIT)
            gone = not any(Path(f"/proc/{pid}").exists() for pid in pids)
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
            except OSError:
                released = False
            else:
                released = True
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=WAIT)
            for pid in pids:
                _collect_pid(pid)
    return code, gone, released


def test_both_actual_cli_dispatchers_reap_published_master_on_both_signals(
    tmp_path: Path,
) -> None:
    supervisor._require_subreaper()
    observed = {
        (launcher, signum.name): _signal_cli(
            launcher, signum, tmp_path / f"{launcher}-{signum.name}"
        )
        for launcher in ("browser_server_controller", "renewal_runner")
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    assert observed == dict.fromkeys(observed, (130, True, True))


@pytest.fixture
def live_master(tmp_path: Path) -> Iterator[tuple[supervisor.SupervisedMaster, int]]:
    with controller.reserve_listener() as listener:
        port = int(listener.getsockname()[1])
        master = supervisor.start_master(
            [sys.executable, "-c", "import signal; signal.pause()"],
            {},
            tmp_path / "server.log",
            owner=[],
            pass_fds=(listener,),
        )
    try:
        yield master, port
    finally:
        master.terminate()


@pytest.mark.parametrize("stage", ["terminate", "ordinary-bind"])
def test_second_signal_waits_until_teardown_and_proof_are_recorded(
    live_master: tuple[supervisor.SupervisedMaster, int],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    master, port = live_master
    delivered: list[tuple[int, bool]] = []
    queued_at_cut: list[bool] = []

    def receive(signum: int, frame: object) -> Never:
        delivered.append((signum, master.release_checked))
        runner._interrupted(signum, frame)

    def queue_second() -> None:
        if len(delivered) == 1:
            signal.raise_signal(signal.SIGINT)
            queued_at_cut.append(signal.SIGINT in signal.sigpending())

    finish = master.terminate
    bind = supervisor._ordinary_bind_error

    def terminate() -> None:
        if stage == "terminate":
            queue_second()
        finish()

    def ordinary_bind(number: int) -> OSError | None:
        if stage == "ordinary-bind":
            queue_second()
        return bind(number)

    monkeypatch.setattr(master, "terminate", terminate)
    monkeypatch.setattr(supervisor, "_ordinary_bind_error", ordinary_bind)
    previous_term = signal.signal(signal.SIGTERM, receive)
    previous_int = signal.signal(signal.SIGINT, receive)
    try:
        with pytest.raises(runner.RenewalInterruptError) as first:
            signal.raise_signal(signal.SIGTERM)
        with (
            supervisor.preserve_cleanup_error(first.value, "second cancellation"),
            pytest.raises(runner.RenewalInterruptError),
        ):
            supervisor.teardown_master(master, port)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    assert queued_at_cut == [True]
    assert delivered == [(signal.SIGTERM, False), (signal.SIGINT, True)]
    assert master.process.poll() is not None
    assert bind(port) is None


def test_exception_before_first_bind_cannot_record_success(
    live_master: tuple[supervisor.SupervisedMaster, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, port = live_master
    fault = RuntimeError("injected pre-bind proof fault")

    def proof_fault(_port: int) -> Never:
        raise fault

    with monkeypatch.context() as patch:
        patch.setattr(supervisor, "require_released_port", proof_fault)
        with pytest.raises(
            supervisor.SupervisorError, match="proof did not complete"
        ) as caught:
            supervisor.teardown_master(master, port)
        assert caught.value.__cause__ is fault
        assert master.release_checked is False
    assert master.process.poll() is not None
    with socket.socket() as competitor:
        competitor.bind(("127.0.0.1", port))
        competitor.listen()
        with pytest.raises(supervisor.SupervisorError, match="port was not released"):
            supervisor.teardown_master(master, port)
        assert master.release_checked is False
    supervisor.teardown_master(master, port)
    assert master.release_checked is True


def _ci_stubs(
    monkeypatch: pytest.MonkeyPatch, run_suite: Callable[..., object]
) -> None:
    monkeypatch.setattr(runner, "SUITES", {"probe": ()})
    monkeypatch.setattr(runner, "_run_browser_suite", run_suite)
    for name in runner.CI_GATES[:-1]:
        monkeypatch.setattr(runner, f"_gate_{name.replace('-', '_')}", lambda *_: [])


def test_exclusive_aggregate_report_keeps_the_primary_teardown_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "ci-report.json"
    report.write_text("preexisting\n", encoding="utf-8")
    primary = supervisor.SupervisorError("primary reap failure")

    def suite(*args: object) -> Never:
        log = Path(str(args[2])) / "server.log"
        log.write_text("ordinary shutdown detail", encoding="utf-8")
        with controller.with_server_log(log):
            raise primary

    _ci_stubs(monkeypatch, suite)
    code = runner.main(["ci", "--artifact-root", str(tmp_path)])
    stderr = capsys.readouterr().err
    assert code == 2
    assert stderr.startswith(
        "renewal-runner: browser supervisor rejected: primary reap failure"
    )
    assert "server log tail: ordinary shutdown detail" in stderr
    assert "CI report publication failed: FileExistsError" in stderr
    assert report.read_text() == "preexisting\n"
    assert "primary reap failure" in (tmp_path / "ci-probe" / "report.json").read_text()


def test_failed_pytest_junit_stays_primary_through_reap_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REALTIME_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv(
        "CLINIC_RENEWAL_APP_DATABASE_URL",
        f"postgresql://clinic_app:{uuid4().hex}@127.0.0.1:5/fixture",
    )
    monkeypatch.setattr(runner, "_resolve_browser", lambda *_: "/unused")
    monkeypatch.setattr(runner, "_capture_source", lambda *_: ({}, "fixture"))
    monkeypatch.setattr(runner, "_require_app_role", lambda *_: None)
    monkeypatch.setattr(runner, "wait_until_ready", lambda *_: None)
    monkeypatch.setattr(
        runner, "realtime_broker", lambda *_, **__: contextlib.nullcontext()
    )
    monkeypatch.setattr(runner, "broker_tap", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(
        runner, "browser_endpoint", lambda *_, **__: contextlib.nullcontext(48000)
    )
    suite = "journey-first-day"
    shutdown_error = supervisor.SupervisorError("shutdown refused")

    def start(
        *_args: object, owner: list[supervisor.SupervisedMaster], **_kwargs: object
    ) -> supervisor.SupervisedMaster:
        master = cast(
            "supervisor.SupervisedMaster",
            Mock(spec=supervisor.SupervisedMaster, process=None),
        )
        owner.append(master)
        (tmp_path / "server.log").write_text("test server tail", encoding="utf-8")
        return master

    def fail_suite(*_args: object, **_kwargs: object) -> int:
        (tmp_path / "browser" / f"junit-{suite}.xml").write_text(
            '<testsuite tests="1" failures="1" errors="0" skipped="0"/>',
            encoding="utf-8",
        )
        return 1

    def fail_reap(*_args: object, **_kwargs: object) -> Never:
        raise shutdown_error

    monkeypatch.setattr(runner, "start_master", start)
    monkeypatch.setattr(runner, "_run_bounded", fail_suite)
    monkeypatch.setattr(runner, "_terminate_master", fail_reap)
    with pytest.raises(runner.RenewalRunnerError, match="suite failed") as caught:
        runner._run_browser_suite(ROOT, suite, tmp_path, tmp_path, None)
    rendered = supervisor.error_details(caught.value)
    assert "shutdown refused" in rendered
    assert "server log tail: test server tail" in rendered


@pytest.mark.parametrize("sidecar", [False, True], ids=["suite", "asgi"])
def test_signal_after_real_sibling_spawn_cannot_escape_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: bool
) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def spawn(_argv: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        process = original(
            [
                sys.executable,
                "-c",
                "import signal; "
                "signal.pthread_sigmask(signal.SIG_SETMASK, []); signal.pause()",
            ],
            cwd=ROOT,
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        spawned.append(process)
        signal.raise_signal(signal.SIGTERM)
        return process

    def launch() -> None:
        if sidecar:
            with realtime_stack.RealtimeStack(ROOT, {}, tmp_path, 9).run():
                pytest.fail("ASGI startup continued after cancellation")
        else:
            runner._run_bounded(["unused"], {}, tmp_path / "suite.log", timeout=WAIT)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    previous = signal.signal(signal.SIGTERM, runner._interrupted)
    try:
        with pytest.raises(runner.RenewalInterruptError):
            launch()
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
    finally:
        signal.signal(signal.SIGTERM, previous)
        for child in spawned:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=WAIT)
            if child.stdout is not None:
                child.stdout.close()


@pytest.mark.parametrize("readonly", [False, True])
def test_primary_malformed_notes_do_not_drop_any_diagnostic(
    tmp_path: Path, readonly: bool
) -> None:
    if readonly:
        error_type = type(
            "ReadOnlyNotesError",
            (ValueError,),
            {"__notes__": property(lambda _: "not writable")},
        )
        error = error_type("primary failure")
    else:
        error = ValueError("primary failure")
        vars(error)["__notes__"] = None
    log = tmp_path / "server.log"
    log.write_text("ordinary server tail", encoding="utf-8")

    def fail(message: str) -> Never:
        raise OSError(message)

    # Use an identity check; pytest match= would expand the malformed notes.
    with (
        pytest.raises(ValueError, check=lambda caught: caught is error),
        controller.with_server_log(log),
        supervisor.always_cleanup(lambda: fail("second close failed"), "close 2"),
        supervisor.always_cleanup(lambda: fail("first close failed"), "close 1"),
    ):
        raise error
    rendered = supervisor.error_details(error)
    assert rendered.splitlines() == [
        "primary failure",
        "close 1: OSError: first close failed",
        "close 2: OSError: second close failed",
        "server log tail: ordinary server tail",
    ]


def test_literal_and_encoded_secrets_do_not_reach_any_diagnostic_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Fixed punctuation corpus; actual synthetic values are created in memory.
    values = [
        f"{index:02}probe{char}private{index:02}"
        for index, char in enumerate(",@:/=&%#? '\"")
    ]
    names = [
        "PGPASSWORD",
        "APP_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "TEST_SUPERUSER_DATABASE_URL",
        "REDISCLI_AUTH",
        "REALTIME_REDIS_URL",
        "CLINIC_OTP_SEED",
        "CLINIC_SESSION_KEY",
        "CLINIC_BROWSER_READINESS_KEY",
        "CLINIC_BROWSER_PASSWORD",
        "CLINIC_BROWSER_PHYSICIAN_PASSWORD",
        "SECRET_KEY",
    ]
    encoded = [quote(value, safe="") for value in values]
    for name, literal, percent in zip(names, values, encoded, strict=True):
        if name.endswith("_URL"):
            monkeypatch.setenv(name, f"postgresql://role:{percent}@127.0.0.1:5/fixture")
        else:
            monkeypatch.setenv(name, literal)
    diagnostic = "\n".join(
        f"context begins {literal} {percent} context ends\n"
        f"PGPASSWORD={literal} plain suffix\n"
        f"target postgresql://role:{percent}@127.0.0.1:5/fixture route"
        for literal, percent in zip(values, encoded, strict=True)
    )
    # The emitted corpus travels only through the child environment, not argv.
    unknown_assignment = "PGPASSWORD=unknown,value@:/=&%#? non-secret after"

    def suite(*args: object) -> Never:
        log = Path(str(args[2])) / "server.log"
        with controller.with_server_log(log):
            master = supervisor.start_master(
                [
                    sys.executable,
                    "-c",
                    "import os; print('untouched log banner'); "
                    "print(os.environ['PROBE_TEXT'])",
                ],
                {**os.environ, "PROBE_TEXT": f"{diagnostic}\n{unknown_assignment}"},
                log,
                owner=[],
            )
            try:
                assert master.process.wait(timeout=WAIT) == 0
            finally:
                master.terminate()
            timeout = subprocess.TimeoutExpired("safe probe", 1)
            timeout.add_note(f"timeout diagnostic:\n{diagnostic}\n{unknown_assignment}")
            message = "test timeout"
            raise supervisor.SupervisorError(message) from timeout

    _ci_stubs(monkeypatch, suite)
    assert runner.main(["ci", "--artifact-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    output = {
        "server log": (tmp_path / "ci-probe" / "server.log").read_text(),
        "suite receipt": json.loads(
            (tmp_path / "ci-probe" / "report.json").read_text()
        )["error"],
        "aggregate receipt": json.loads((tmp_path / "ci-report.json").read_text())[
            "gates"
        ]["browser"][0].get("error", ""),
        "aggregate stdout": captured.out,
    }
    leaks = [
        (where, index)
        for where, text in output.items()
        for index, value in enumerate([*values, *encoded, "value@:/=&%#?"])
        if value in text
    ]
    assert not leaks, f"leaked corpus positions: {leaks}"
    for name in ("server log", "suite receipt", "aggregate receipt"):
        assert "context begins [REDACTED] [REDACTED] context ends" in output[name]
        assert "PGPASSWORD=[REDACTED] non-secret after" in output[name]
        assert "untouched log banner" in output[name]
    assert captured.err == ""


def test_harness_writes_and_release_flag_pass_through_their_single_boundaries() -> None:
    """Best-effort AST tripwire, not a proof over dynamic dispatch or subprocesses."""
    names = (
        "browser_server_controller",
        "browser_server_supervisor",
        "renewal_runner",
        "realtime_stack",
        "sentinel_runtime",
    )
    writes: list[tuple[str, str]] = []
    flags: list[tuple[str, str]] = []
    for name in names:
        source = ROOT / "ops" / "testing" / f"{name}.py"
        text = source.read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            match node:
                case ast.Attribute(attr="release_checked", ctx=ast.Store()):
                    flags.append((name, lines[node.lineno - 1].strip()))
                case ast.Call(
                    func=ast.Name(id="setattr" | "delattr"),
                    args=[_, ast.Constant(value="release_checked"), *_],
                ):
                    flags.append((name, ast.unparse(node)))
                case ast.Call(
                    func=ast.Attribute(
                        attr="write_text" | "write_bytes" | "write" | "writelines"
                    )
                    | ast.Name(id="print" | "write_no_replace"),
                ):
                    writes.append((name, ast.unparse(node.func)))
                case ast.Call(
                    func=ast.Attribute(attr="open"), args=[ast.Constant(value=mode)]
                ) if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                    writes.append((name, ast.unparse(node)))
                case _:
                    continue
    # An allowlist and private KEK are runtime inputs, and wfile is HTTP traffic.
    assert writes == [
        ("renewal_runner", "write_no_replace"),
        ("renewal_runner", "allowlist.write_text"),
        ("realtime_stack", "self.wfile.write"),
    ]
    assert sorted(flags) == [
        ("browser_server_supervisor", "master.release_checked = True"),
        ("browser_server_supervisor", "self.release_checked = False"),
    ]
