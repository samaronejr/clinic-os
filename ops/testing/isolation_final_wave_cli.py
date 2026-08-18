"""Define the closed final-wave forms and their allowlisted child programs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type Form = Literal["inputs", "scope-pre", "pre-f4", "final"]


@dataclass(frozen=True, slots=True)
class FinalWaveInvocation:
    """One exact final-wave form and immutable argument tuple."""

    form: Form
    sha: str
    arguments: tuple[str, ...]


def child_program(form: Form) -> Path:
    """Map each controller form only to its allowlisted decision child."""
    names = {
        "inputs": "freeze_final_wave_inputs.py",
        "scope-pre": "scope_gate.sh",
        "pre-f4": "freeze_final_wave_outputs.py",
        "final": "final_artifact_gate.sh",
    }
    return Path(__file__).with_name(names[form])
