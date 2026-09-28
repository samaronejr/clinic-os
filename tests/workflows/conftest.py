"""Opt-in decision mutant hook; inert unless CLINIC_WORKFLOW_DECISION_MUTANT is set."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from workflows.decision_sites import rewritten

if TYPE_CHECKING:
    from collections.abc import Iterator

MUTANT = "CLINIC_WORKFLOW_DECISION_MUTANT"


@pytest.fixture(autouse=True)
def workflow_decision_mutant() -> Iterator[None]:
    """Apply '<site id>|<mode>' rewrites (';;'-joined); unset: shipped code runs."""
    requested = os.environ.get(MUTANT)
    if not requested:
        yield
        return
    mutations = frozenset(
        (site, mode)
        for site, _separator, mode in (
            item.rpartition("|") for item in requested.split(";;")
        )
    )
    with rewritten(mutations):
        yield
