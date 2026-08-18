from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing import final_wave_controller
from ops.testing.isolation_common import IsolationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def test_final_artifact_gate_has_no_terminal_destination_capability() -> None:
    # Given: the claim-staged F4 decision child.
    script = PROJECT_ROOT / "ops/testing/final_artifact_gate.sh"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")

    # When / Then: it can stage a decision but cannot name final destinations.
    assert "F4-final.txt" in source
    assert "F4-outcome.json" in source
    for forbidden in (
        "--evidence",
        "--final-inventory",
        "--control-root",
        "final.json",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "inputs",
            "--sha",
            SHA,
            "--control-root",
            final_wave_controller.CONTROL,
            "--output",
            final_wave_controller.INPUTS,
        ],
        [
            "scope-pre",
            "--sha",
            SHA,
            "--inputs",
            final_wave_controller.INPUTS,
            "--control-root",
            final_wave_controller.CONTROL,
            "--output",
            f"{final_wave_controller.TERMINAL}/F4-pre.txt",
        ],
        [
            "pre-f4",
            "--sha",
            SHA,
            "--inputs",
            final_wave_controller.INPUTS,
            "--control-root",
            final_wave_controller.CONTROL,
            "--namespace",
            final_wave_controller.TERMINAL,
            "--output",
            f"{final_wave_controller.CONTROL}/pre-f4.json",
        ],
        [
            "final",
            "--sha",
            SHA,
            "--inputs",
            final_wave_controller.INPUTS,
            "--pre-f4",
            f"{final_wave_controller.CONTROL}/pre-f4.json",
            "--control-root",
            final_wave_controller.CONTROL,
        ],
    ],
)
def test_final_wave_controller_accepts_only_exact_closed_forms(
    arguments: list[str],
) -> None:
    invocation = final_wave_controller.parse_invocation(arguments)
    assert invocation.sha == SHA


def test_final_wave_controller_rejects_caller_selected_evidence() -> None:
    with pytest.raises(IsolationError, match="closed form"):
        final_wave_controller.parse_invocation(
            [
                "scope-pre",
                "--sha",
                SHA,
                "--inputs",
                final_wave_controller.INPUTS,
                "--evidence",
                "/var/empty/alias",
                "--output",
                f"{final_wave_controller.TERMINAL}/F4-pre.txt",
            ]
        )
