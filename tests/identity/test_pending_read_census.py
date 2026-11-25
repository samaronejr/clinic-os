"""These non-export read boundaries need observed calls, not inventory labels."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from identity.ai_authority_census import exercise_pending_read_authority
from identity.cut1_authority_census import ExecutedProofs
from rbac_fixtures import RbacGraph
from rbac_fixtures import rbac_graph as seed_graph

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.django_db(transaction=True)
def test_pending_read_census_observes_three_refusal_paths(
    synthetic_secret_backend: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    symbols = {
        "apps.ai.pending_access._command_record",
        "apps.ai.care_views._preview",
        "apps.ai.pending_outputs.ai_pending_visible",
        "apps.ai.pending_outputs.decisions_visible",
        "apps.ai.pending_outputs.reselect",
        "apps.ai.pending_outputs.withheld_artifact",
    }
    proofs = ExecutedProofs(symbols)

    def graph() -> RbacGraph:
        result = seed_graph._get_wrapped_function()(synthetic_secret_backend)
        assert isinstance(result, RbacGraph)
        return result

    exercise_pending_read_authority(
        proofs, graph, monkeypatch, synthetic_secret_backend
    )
    assert proofs.executed() == {symbol + "#cut1" for symbol in symbols}
