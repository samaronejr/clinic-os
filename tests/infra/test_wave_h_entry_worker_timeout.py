"""The differential worker must report a stalled verifier as a failure."""

import faulthandler
from pathlib import Path
from unittest.mock import Mock

import pytest

from infra import wave_h_entry_worker as worker


def test_verifier_watchdog_is_fatal_and_always_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm = Mock()
    cancel = Mock()
    error = RuntimeError("verifier failed")
    monkeypatch.setattr(faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", cancel)
    monkeypatch.setattr(worker, "_verify", Mock(side_effect=error))

    with pytest.raises(RuntimeError, match="verifier failed"):
        worker._guarded_verify(Path("verify.py"), {})

    arm.assert_called_once_with(worker.VERDICT_TIMEOUT_SECONDS, exit=True)
    cancel.assert_called_once_with()
