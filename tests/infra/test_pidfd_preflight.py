from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing.pidfd_preflight import main

if TYPE_CHECKING:
    from types import ModuleType


def test_preflight_reports_the_selected_interpreter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"pidfd preflight passed: Python {sys.version.split()[0]}" in captured.out
    assert str(Path(sys.executable).resolve()) in captured.out


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        (os, "pidfd_open"),
        (os, "waitid"),
        (os, "P_PIDFD"),
        (signal, "pidfd_send_signal"),
    ],
)
def test_preflight_refuses_each_missing_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: ModuleType,
    attribute: str,
) -> None:
    monkeypatch.delattr(module, attribute)

    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(Path(sys.executable).resolve()) in captured.err
    assert (
        f"lacks required Linux pidfd APIs: {module.__name__}.{attribute}"
        in captured.err
    )
