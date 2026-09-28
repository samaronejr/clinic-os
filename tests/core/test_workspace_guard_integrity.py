"""Subprocess proofs: genuine middleware observation, tampering and vacuity."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import Client, override_settings
from ops.testing.process_helpers import run_process
from ops.testing.renewal_acceptance import _junit_counts
from ops.testing.runtime_paths import runtime_directory

from workspace_refusal_observer import RefusalMiddleware

# Reviewer bypasses plus one further dependency bound by the observer.
DEPENDENCY_PATCHES = {
    "check-noop": ("workspace_refusal_observer", "check_refusal", "lambda *a: None"),
    "scope-off": ("workspace_refusal_support", "workspace_scope", "lambda *a: False"),
    "resolve-off": ("workspace_refusal_support", "resolve", "lambda *a, **k: None"),
}
# In-place rewrites of the frozen guard (round-8 reviewer), then ones that
# reach the observer and name attributes dynamically, or also erase the record
# or the verifier. These plugins live outside tests/, so the static guard
# (test_workspace_guard_static.py) never sees them: the runtime check must.
# Each maps to the fixture body and the guard failures it must name.
_REACH = (
    "    from conftest import REFUSAL_OBSERVER\n"
    "    observer = request.config.stash[REFUSAL_OBSERVER]\n"
)
_DYNAMIC_REACH = (
    "    import gc\n"
    "    observer = next(o for o in gc.get_objects() "
    "if type(o).__name__ == 'Refusal' + 'Observer')\n"
)
_DYNAMIC_CHECK_CODE = (
    "    monkeypatch.setattr(getattr(observer, 'ch' + 'eck'), '__co' + 'de__', "
    "(lambda *a: None).__code__)\n"
)
_REWRITTEN = "Refusal guard function rewritten: "
_ROOT = "Refusal guard root changed: "
_UNCHECKED = (
    "Test client requests, evaluated and checked responses differ: "
    "requests=1 evaluated=1 checked=0"
)
REWRITES = {
    "frozen-check-code": (
        _REACH + "    monkeypatch.setattr(observer.check, '__code__', "
        "(lambda *a: None).__code__)\n",
        (_REWRITTEN + "check_refusal.__code__", _UNCHECKED),
    ),
    "frozen-scope-code": (
        _REACH
        + "    monkeypatch.setattr(observer.check.__globals__['workspace_scope'], "
        "'__code__', (lambda *a: False).__code__)\n",
        (_REWRITTEN + "workspace_scope.__code__",),
    ),
    "frozen-namespace": (
        _REACH + "    monkeypatch.setitem(observer.check.__globals__, "
        "'workspace_scope', lambda *a: False)\n",
        (
            _REWRITTEN + "check_refusal.__globals__",
            _REWRITTEN + "workspace_scope.__globals__",
        ),
    ),
    "frozen-builtin": (
        _REACH + "    monkeypatch.setitem(observer.check.__globals__, "
        "'getattr', lambda *a: None)\n",
        (
            _REWRITTEN + "check_refusal.__globals__",
            _REWRITTEN + "check_refusal.__builtins__",
            _REWRITTEN + "workspace_scope.__globals__",
        ),
    ),
    "dynamic-name": (
        _DYNAMIC_REACH + _DYNAMIC_CHECK_CODE,
        (_REWRITTEN + "check_refusal.__code__", _UNCHECKED),
    ),
    "erase-record": (
        _DYNAMIC_REACH
        + "    monkeypatch.setattr(observer, 'function_' + 'states', ())\n"
        + _DYNAMIC_CHECK_CODE,
        (_ROOT + "RefusalObserver.function_states", _UNCHECKED),
    ),
    "rewrite-verifier": (
        _DYNAMIC_REACH
        + "    states = getattr(observer, 'function_' + 'states')\n"
        + "    monkeypatch.setattr(type(states[0]).changed, '__co' + 'de__', "
        "(lambda self: []).__code__)\n" + _DYNAMIC_CHECK_CODE,
        (_ROOT + "_FunctionState.changed.__code__", _UNCHECKED),
    ),
}
FAILING = {
    "write",
    "swallow",
    "unwrap",
    "disabled",
    "restored",
    *DEPENDENCY_PATCHES,
    *REWRITES,
}


@override_settings(
    ROOT_URLCONF="workspace_guard_probe_urls",
    MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware"],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
def test_guard_canary() -> None:
    mode = os.environ.get("WORKSPACE_GUARD_PROBE", "normal")
    if mode == "no-client":
        return
    client = Client()
    root = f"/observer/{uuid4()}/"
    if mode == "restored":
        # Catch the missing receipt, then restore the observer before teardown.
        # Its latched failure must still belong to this individual test.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                RefusalMiddleware,
                "process_response",
                lambda self, request, response: response,
            )
            with pytest.raises(pytest.fail.Exception):
                client.get(root + "404/")
        return
    if mode in {"write", "swallow", *DEPENDENCY_PATCHES, *REWRITES}:
        if mode == "swallow":
            # An adversarial caller catches the immediate failure. The report
            # and session still have to fail from the latched violation.
            try:
                client.post(root + "403/", {"write": "yes"})
            except BaseException as error:
                if not isinstance(error, pytest.fail.Exception):
                    raise
        else:
            client.post(root + "403/", {"write": "yes"})
        return
    assert client.get(root + "200/").status_code == 200
    if mode != "success-only":
        assert client.get(root + "404/").status_code == 404


def _plant(mode: str) -> str:
    """The plugin a mode loads; its autouse fixture is the planted bypass."""
    fixture = "@pytest.fixture(autouse=True)\ndef bypass(monkeypatch, request):\n"
    if mode == "unwrap":
        return (
            "from django.test import Client\n" + fixture + "    "
            "monkeypatch.setattr(Client, 'request', Client.request.__wrapped__)\n"
        )
    if mode == "disabled":
        return fixture + (
            "    from workspace_refusal_observer import RefusalMiddleware\n"
            "    monkeypatch.setattr(RefusalMiddleware, 'process_response', "
            "lambda self, request, response: response)\n"
        )
    if mode in DEPENDENCY_PATCHES:
        module, name, value = DEPENDENCY_PATCHES[mode]
        return fixture + (
            f"    import {module}\n"
            f"    monkeypatch.setattr({module}, {name!r}, {value})\n"
        )
    if mode in REWRITES:
        return fixture + REWRITES[mode][0]
    return ""


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "write",
        "swallow",
        "unwrap",
        "disabled",
        "restored",
        *DEPENDENCY_PATCHES,
        *REWRITES,
        "success-only",
        "no-client",
    ],
)
def test_guard_is_mandatory_in_real_pytest_sessions(tmp_path: Path, mode: str) -> None:
    with runtime_directory(tmp_path, purpose="observer") as directory:
        plugin = directory / "observer_plant.py"
        plugin.write_text("import pytest\n" + _plant(mode))
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("DJANGO_SETTINGS_MODULE", "config.settings.test")
            environment.setenv("WORKSPACE_GUARD_PROBE", mode)
            environment.setenv(
                "PYTHONPATH",
                os.pathsep.join((str(directory), str(Path.cwd() / "tests"))),
            )
            completed = run_process(
                (
                    sys.executable,
                    "-m",
                    "pytest",
                    "--reuse-db",
                    "-q",
                    "-p",
                    "observer_plant",
                    "-o",
                    "junit_family=xunit1",
                    "--basetemp=" + str(directory / "b"),
                    "--junitxml=" + str(directory / "junit.xml"),
                    "--tb=short",
                    "tests/core/test_workspace_guard_integrity.py::test_guard_canary",
                ),
                timeout_seconds=120,
            )
        tests, failures, errors, skipped = _junit_counts(directory / "junit.xml")
    assert tests == 1
    assert errors == skipped == 0
    assert failures == int(mode in FAILING)
    lines = completed.stdout.splitlines()
    stats = json.loads(
        next(
            line.removeprefix("REFUSAL_GUARD ")
            for line in lines
            if line.startswith("REFUSAL_GUARD ")
        )
    )
    expected = 0 if mode in {"normal", "no-client"} else 1
    assert completed.returncode == expected, completed.stdout + completed.stderr
    if mode == "normal":
        assert stats["responses"] == stats["client_requests"] == 2
        assert stats["refusals"] == 1
    elif mode in {"write", "swallow", *DEPENDENCY_PATCHES}:
        # The frozen check still runs and fails; teardown also names the patch.
        assert stats["violations"] == 1
        assert stats["responses"] == stats["evaluated_requests"] == 1
        if mode in DEPENDENCY_PATCHES:
            module, name, _ = DEPENDENCY_PATCHES[mode]
            assert (
                f"REFUSAL_GUARD_FAILURE Refusal guard dependency replaced: "
                f"{module}.{name}"
            ) in lines
    elif mode in REWRITES:
        # The rewrite silences the check itself (a shadowed builtin leaves the
        # cookie test standing); content integrity, the root seal and the
        # per-test count still fail this test and name the cause.
        assert stats["violations"] == int(mode == "frozen-builtin")
        assert stats["client_requests"] == stats["evaluated_requests"] == 1
        messages = REWRITES[mode][1]
        assert stats["responses"] == int(_UNCHECKED not in messages)
        for message in messages:
            assert f"REFUSAL_GUARD_FAILURE {message}" in lines
    elif mode == "unwrap":
        # The wrapper was removed, but the actual response observer still ran.
        assert stats["responses"] == 2
        assert stats["refusals"] == 1
    elif mode in {"disabled", "restored"}:
        assert stats["client_requests"] > 0
        assert stats["responses"] == stats["refusals"] == 0
        assert stats["evaluated_requests"] == 0
        assert (
            "REFUSAL_GUARD_FAILURE Client request was not evaluated exactly once: "
            "started=1 evaluated=0"
        ) in lines
    elif mode == "success-only":
        assert stats["client_requests"] == stats["responses"] == 1
        assert stats["refusals"] == 0
    else:
        assert stats["client_requests"] == stats["responses"] == 0


@pytest.mark.parametrize("mode", ["normal", "success-only"])
def test_distributed_sessions_check_the_merged_worker_receipts(
    tmp_path: Path, mode: str
) -> None:
    # xdist drops a worker's session exit status (the hosted job's parallel
    # phase); the controller must merge every worker's receipt and apply the
    # session checks to the totals. success-only is vacuous: no refusal ran.
    with (
        runtime_directory(tmp_path, purpose="observer") as directory,
        pytest.MonkeyPatch.context() as environment,
    ):
        environment.setenv("DJANGO_SETTINGS_MODULE", "config.settings.test")
        environment.setenv("WORKSPACE_GUARD_PROBE", mode)
        environment.setenv("PYTHONPATH", str(Path.cwd() / "tests"))
        completed = run_process(
            (
                sys.executable,
                "-m",
                "pytest",
                "--reuse-db",
                "-q",
                "--numprocesses=1",
                "--basetemp=" + str(directory / "b"),
                "tests/core/test_workspace_guard_integrity.py::test_guard_canary",
            ),
            timeout_seconds=120,
        )
    lines = completed.stdout.splitlines()
    stats = json.loads(
        next(
            line.removeprefix("REFUSAL_GUARD ")
            for line in lines
            if line.startswith("REFUSAL_GUARD ")
        )
    )
    requests = 2 if mode == "normal" else 1
    assert stats["client_requests"] == stats["evaluated_requests"] == requests
    assert stats["responses"] == requests
    assert stats["refusals"] == int(mode == "normal")
    expected = 0 if mode == "normal" else 1
    assert completed.returncode == expected, completed.stdout + completed.stderr
    vacuous = (
        "REFUSAL_GUARD_FAILURE Django client ran but refusal observer saw zero refusals"
    )
    assert (vacuous in lines) == (mode == "success-only")
