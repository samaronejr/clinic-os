"""Three-way section comparison; only a clinician may apply its result."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SectionMerge:
    """A conflict always retains the clinician's current text."""

    state: Literal["converged", "unchanged", "apply", "conflict", "stale"]
    text: str


def merge_section(
    *,
    base: str,
    model: str,
    current: str,
    edit_epochs: tuple[int, int],
    dependencies_current: bool = True,
) -> SectionMerge:
    """Notice edit/revert and deletion even when the current bytes equal base."""
    if not dependencies_current:
        return SectionMerge("stale", current)
    if model == current:
        return SectionMerge("converged", current)
    if model == base:
        return SectionMerge("unchanged", current)
    if edit_epochs[1] != edit_epochs[0] or current != base:
        return SectionMerge("conflict", current)
    return SectionMerge("apply", model)
