"""Todo 42's 54 three-way cases include deletion and edit-then-revert."""

from __future__ import annotations

from itertools import product

import pytest
from apps.scribe.merge import merge_section

VALUES = ("", "first", "second")
UNTOUCHED = (
    (
        "converged",
        "unchanged",
        "unchanged",
        "apply",
        "converged",
        "conflict",
        "apply",
        "conflict",
        "converged",
    ),
    (
        "converged",
        "apply",
        "conflict",
        "unchanged",
        "converged",
        "unchanged",
        "conflict",
        "apply",
        "converged",
    ),
    (
        "converged",
        "conflict",
        "apply",
        "conflict",
        "converged",
        "apply",
        "unchanged",
        "unchanged",
        "converged",
    ),
)
CASES = tuple(product(range(3), range(3), range(3), (False, True)))


@pytest.mark.parametrize(("base", "model", "current", "touched"), CASES)
def test_54_three_way_merge_cases(
    base: int, model: int, current: int, *, touched: bool
) -> None:
    expected = UNTOUCHED[base][model * 3 + current]
    if touched and expected == "apply":
        expected = "conflict"
    result = merge_section(
        base=VALUES[base],
        model=VALUES[model],
        current=VALUES[current],
        edit_epochs=(7, 9 if touched else 7),
    )
    assert result.state == expected
    assert result.text == (VALUES[model] if expected == "apply" else VALUES[current])


def test_changed_dependency_never_becomes_an_applicable_suggestion() -> None:
    result = merge_section(
        base="",
        model="first",
        current="",
        edit_epochs=(0, 0),
        dependencies_current=False,
    )
    assert (result.state, result.text) == ("stale", "")
