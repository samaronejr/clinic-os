"""Alias SQL must remain inside the finite source-discovery boundary."""

from pathlib import Path

import pytest
from apps.intake.migrations._alias_scope_sql import SQL

from identity.authority_sql import references
from scheduling.clock_boundary import sql_boundary_violations
from scheduling.clock_runsql import SQLDecoder
from scheduling.clock_source import parsed_source, source_fragments

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative",
    [
        "apps/intake/alias_access.py",
        "apps/intake/migrations/0015_patient_aliases.py",
    ],
)
def test_alias_sql_is_finitely_discoverable(relative: str) -> None:
    path = ROOT / relative
    decoder = SQLDecoder(ROOT)
    tree = parsed_source(path.read_text())

    fragments = [
        *source_fragments(tree, decoder.bindings(path)),
        *decoder.fragments(path, tree),
    ]

    violations = set().union(*(sql_boundary_violations(value) for value in fragments))
    assert violations == set()


def test_alias_lineage_sql_preserves_observable_authority_calls() -> None:
    body = (
        SQL.partition("CREATE FUNCTION clinic_app.patient_alias_set(")[2]
        .partition("AS $function$")[2]
        .partition("$function$;")[0]
    )
    parsed = references(body, syntax_keywords=frozenset())

    assert parsed.calls.isdisjoint({("forward",), ("root",), ("members",)})
    assert ("current_setting",) in parsed.calls
    assert ("statement_timestamp",) in parsed.calls
    assert "app.current_user_id" in parsed.settings
