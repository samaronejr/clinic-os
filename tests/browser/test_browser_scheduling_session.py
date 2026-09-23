from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing.browser_artifact_publisher import ArtifactPublicationError
from ops.testing.browser_server_controller import (
    AVAILABLE_SUITES,
    parse_suite_arguments,
)
from ops.testing.browser_server_flow import BrowserFlowError
from ops.testing.image_source import RUNNER_SUITE_PATHS

from browser.browser_session_harness import CAUSAL_CHAIN, run_recorded_session

if TYPE_CHECKING:
    from pathlib import Path

SCHEDULING_REQUIRED: Final = ("availability", "patient", "scheduling")


def test_scheduling_session_requires_all_three_suites_before_browser_code(
    tmp_path: Path,
) -> None:
    journal, effects, read_fd = run_recorded_session(
        tmp_path, required=SCHEDULING_REQUIRED, suite_id="scheduling"
    )

    assert journal.recorded == CAUSAL_CHAIN
    assert "dispatch:scheduling" in effects.calls
    assert effects.published == ("browser/scheduling/summary.json",)
    os.close(read_fd)


def test_a_candidate_missing_scheduling_is_rejected_before_browser_code(
    tmp_path: Path,
) -> None:
    with pytest.raises(BrowserFlowError, match="advertises"):
        run_recorded_session(
            tmp_path,
            frozenset({"suite-ids"}),
            SCHEDULING_REQUIRED,
            "scheduling",
        )


def test_scheduling_is_never_dispatched_outside_the_required_set(
    tmp_path: Path,
) -> None:
    with pytest.raises(BrowserFlowError, match="not part of the required suite set"):
        run_recorded_session(
            tmp_path,
            required=("availability", "patient"),
            suite_id="scheduling",
        )


def test_a_foreign_scheduling_artifact_can_never_be_exported(tmp_path: Path) -> None:
    with pytest.raises(ArtifactPublicationError, match="artifact prefix"):
        run_recorded_session(
            tmp_path,
            frozenset({"foreign-artifact"}),
            SCHEDULING_REQUIRED,
            "scheduling",
        )


def test_the_scheduling_suite_source_and_grammar_are_committed() -> None:
    suite_id, required = parse_suite_arguments(
        [
            "suite",
            "scheduling",
            "--require-suite",
            "availability",
            "--require-suite",
            "patient",
            "--require-suite",
            "scheduling",
        ]
    )

    assert suite_id == "scheduling"
    assert required == sorted(SCHEDULING_REQUIRED)
    assert sorted(SCHEDULING_REQUIRED) == AVAILABLE_SUITES
    assert RUNNER_SUITE_PATHS["scheduling"] == (
        "ops/testing/browser_suites/scheduling.py"
    )
