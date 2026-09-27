"""The resolver-derived refusal-exit gate runs after the HTTP scenario corpus."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from conftest import REFUSAL_OBSERVER

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def test_all_workspace_refusal_exits_are_executed(
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
) -> None:
    trace = request.config.stash[REFUSAL_OBSERVER].trace
    record_property(
        "refusal_exits", json.dumps(sorted(x.location for x in trace.exits))
    )
    record_property(
        "executed_refusal_exits", json.dumps(sorted(x.location for x in trace.seen))
    )
    missing = trace.missing()
    assert not missing, "Unreached refusal exits:\n" + "\n".join(missing)
