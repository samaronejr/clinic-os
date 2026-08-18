from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_scope_gate_tracks_a_closed_empty_false_positive_allowlist() -> None:
    # Given: the committed source-scope gate and its schema-bound allowlist.
    script = PROJECT_ROOT / "ops/testing/scope_gate.sh"
    allowlist = PROJECT_ROOT / "ops/testing/scope_false_positive_allowlist.json"
    assert script.is_file()
    assert allowlist.is_file()

    # When / Then: the initial allowlist is closed and grants no hidden exception.
    value = json.loads(allowlist.read_text(encoding="utf-8"))
    assert value == {"entries": [], "schema_version": 1}
    source = script.read_text(encoding="utf-8")
    assert "--sha" in source
    assert "--inputs" in source
    assert "--evidence" in source
