from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing.browser_artifact_publisher import ArtifactPublicationError
from ops.testing.browser_server_flow import BrowserFlowError

from browser_session_harness import CAUSAL_CHAIN, run_recorded_session

if TYPE_CHECKING:
    from pathlib import Path

AVAILABILITY_REQUIRED: Final = ("availability", "patient")


def test_availability_session_requires_both_suites_before_browser_code(
    tmp_path: Path,
) -> None:
    journal, effects, read_fd = run_recorded_session(
        tmp_path, required=AVAILABILITY_REQUIRED, suite_id="availability"
    )

    assert journal.recorded == CAUSAL_CHAIN
    assert "dispatch:availability" in effects.calls
    assert effects.published == ("browser/availability/summary.json",)
    os.close(read_fd)


def test_missing_availability_entry_is_rejected_before_browser_code(
    tmp_path: Path,
) -> None:
    with pytest.raises(BrowserFlowError, match="advertises"):
        run_recorded_session(
            tmp_path,
            frozenset({"missing-availability"}),
            AVAILABILITY_REQUIRED,
            "availability",
        )


def test_a_suite_outside_the_required_set_is_never_dispatched(
    tmp_path: Path,
) -> None:
    with pytest.raises(BrowserFlowError, match="not part of the required suite set"):
        run_recorded_session(tmp_path, required=("patient",), suite_id="availability")


def test_a_foreign_suite_artifact_can_never_be_exported(tmp_path: Path) -> None:
    with pytest.raises(ArtifactPublicationError, match="artifact prefix"):
        run_recorded_session(
            tmp_path,
            frozenset({"foreign-artifact"}),
            AVAILABILITY_REQUIRED,
            "availability",
        )
