"""Alias SQL must remain inside the finite source-discovery boundary."""

from pathlib import Path

import pytest
from apps.identity.migrations._delegate_sql import SQL as IDENTITY_SQL
from apps.intake.migrations._alias_scope_sql import SQL
from apps.intake.migrations._delegate_session_sql import SQL as AUTHORITY_SQL

from identity.authority_sql import references
from scheduling.clock_boundary import sql_boundary_violations
from scheduling.clock_runsql import SQLDecoder
from scheduling.clock_source import parsed_source, source_fragments

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative",
    [
        "apps/intake/alias_access.py",
        "apps/intake/migrations/0017_patient_aliases.py",
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
    # Wave B delegation (ee794ca) moves expiry behind effective authority.
    # Bind every edge and both clock readers; CTE names must not become calls.
    boundaries: tuple[tuple[str, str, str, set[tuple[str, ...]], set[str]], ...] = (
        (
            SQL,
            "patient_alias_set",
            "$function$",
            {
                ("as",),
                ("clinic_app", "current_patient_authority"),
                ("current_setting",),
                ("exists",),
                ("in",),
                ("max",),
                ("nullif",),
            },
            {
                "app.current_patient_session",
                "app.current_principal",
                "app.current_tenant",
                "app.current_user_id",
            },
        ),
        (
            AUTHORITY_SQL,
            "current_patient_authority",
            "$f$",
            {
                ("clinic_app", "patient_authority"),
                ("current_setting",),
                ("nullif",),
            },
            {"app.current_patient_session", "app.current_user_id"},
        ),
        (
            AUTHORITY_SQL,
            "patient_authority",
            "$f$",
            {
                ("and",),
                ("any",),
                ("array",),
                ("clinic_app", "delegate_now"),
                ("clinic_app", "patient_identity"),
                ("coalesce",),
                ("exists",),
                ("lateral",),
                ("or",),
                ("unnest",),
                ("where",),
            },
            set(),
        ),
        (
            IDENTITY_SQL,
            "patient_identity",
            "$f$",
            {("statement_timestamp",)},
            set(),
        ),
        (
            IDENTITY_SQL,
            "delegate_now",
            "$f$",
            {("statement_timestamp",)},
            set(),
        ),
    )
    for source, function, delimiter, calls, settings in boundaries:
        body = (
            source.partition(f"CREATE FUNCTION clinic_app.{function}(")[2]
            .partition(f"AS {delimiter}")[2]
            .partition(f"{delimiter};")[0]
        )
        parsed = references(body, syntax_keywords=frozenset())

        assert parsed.calls == calls
        assert parsed.settings == settings
        assert parsed.opaque == set()
        assert not parsed.writes
