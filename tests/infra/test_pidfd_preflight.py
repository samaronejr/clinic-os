from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest
from ops.testing.pidfd_preflight import main


def test_preflight_reports_the_selected_interpreter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"pidfd preflight passed: Python {sys.version.split()[0]}" in captured.out
    assert str(Path(sys.executable).resolve()) in captured.out


@pytest.mark.parametrize(
    "attribute",
    [
        "os.pidfd_open",
        "os.waitid",
        "os.P_PIDFD",
        "signal.pidfd_send_signal",
    ],
)
def test_preflight_refuses_each_missing_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    attribute: str,
) -> None:
    # Keep mutation targets explicit for the workspace guard's static scanner.
    match attribute:
        case "os.pidfd_open":
            monkeypatch.delattr(os, "pidfd_open")
        case "os.waitid":
            monkeypatch.delattr(os, "waitid")
        case "os.P_PIDFD":
            monkeypatch.delattr(os, "P_PIDFD")
        case "signal.pidfd_send_signal":
            monkeypatch.delattr(signal, "pidfd_send_signal")
        case _:
            pytest.fail(f"unexpected pidfd API: {attribute}")

    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(Path(sys.executable).resolve()) in captured.err
    assert f"lacks required Linux pidfd APIs: {attribute}" in captured.err
