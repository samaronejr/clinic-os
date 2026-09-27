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
FAILING = {"write", "swallow", "unwrap", "disabled", "restored", *DEPENDENCY_PATCHES}


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
    if mode in {"write", "swallow", *DEPENDENCY_PATCHES}:
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
        "success-only",
        "no-client",
    ],
)
def test_guard_is_mandatory_in_real_pytest_sessions(tmp_path: Path, mode: str) -> None:
    with runtime_directory(tmp_path, purpose="observer") as directory:
        plugin = directory / "observer_plant.py"
        source = "import pytest\n"
        if mode == "unwrap":
            source += (
                "from django.test import Client\n"
                "@pytest.fixture(autouse=True)\n"
                "def bypass(monkeypatch):\n"
                "    monkeypatch.setattr(Client, 'request', "
                "Client.request.__wrapped__)\n"
            )
        if mode == "disabled":
            source += (
                "@pytest.fixture(autouse=True)\n"
                "def bypass(monkeypatch):\n"
                "    from workspace_refusal_observer import RefusalMiddleware\n"
                "    monkeypatch.setattr(RefusalMiddleware, 'process_response', "
                "lambda self, request, response: response)\n"
            )
        if mode in DEPENDENCY_PATCHES:
            module, name, value = DEPENDENCY_PATCHES[mode]
            source += (
                "@pytest.fixture(autouse=True)\n"
                "def bypass(monkeypatch):\n"
                f"    import {module}\n"
                f"    monkeypatch.setattr({module}, {name!r}, {value})\n"
            )
        plugin.write_text(source)
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
