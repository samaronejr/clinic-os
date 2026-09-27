"""Pin the server-derived, type-independent datetime literal value boundary."""

import pytest

from scheduling.clock_temporal_inputs import LiteralInputs

from .clock_datetime_tokens import special_datetime_tokens

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


@pytest.mark.parametrize(
    ("source", "risky"),
    [
        ("SELECT 'not temporal'::text", False),
        ("DECLARE x text := 'now'; BEGIN RETURN x; END", True),
        ("SELECT '2001-01-01'::timestamptz", False),
        ("SELECT CAST('now' AS timestamp(3) with time zone)", True),
        ('''SELECT 'now'::pg_catalog . "timestamptz"''', True),
        (
            "DECLARE x timestamp with time zone NOT NULL := 'now'; BEGIN RETURN x; END",
            True,
        ),
        ("SELECT $value$now$value$::timestamptz", True),
    ],
)
def test_temporal_input_boundary(source: str, *, risky: bool) -> None:
    assert LiteralInputs().unresolved(source) is risky


def test_postgresql16_derives_the_exact_special_clock_values() -> None:
    # Only a cross-check: production classification derives the vocabulary from
    # the server executable and validates every candidate through its input API.
    assert special_datetime_tokens() == {"now", "today", "tomorrow", "yesterday"}


@pytest.mark.parametrize(
    "source",
    [
        r"SELECT E'\156ow'",
        r"SELECT U&'\006eow'",
        "SELECT U&'!006eow' UESCAPE '!'",
        "SELECT 'to'\n'day'",
        "SELECT $literal$  YeStErDaY $literal$",
        "SELECT '   NoWhErE'",
    ],
)
def test_decoded_values_not_type_adjacency(source: str) -> None:
    assert LiteralInputs().counts(source) == {"unresolved-temporal-input": 1}


def test_literal_exceptions_are_exact_and_limited_to_python_mapping_keys() -> None:
    boundary = LiteralInputs()
    assert set(boundary.allowlist) == {"today_url"}
    assert not boundary.python_risky("today_url", mapping_key=True)
    assert boundary.python_risky("today_url", mapping_key=False)
    assert boundary.risky("today_url")
    assert boundary.python_risky("today_url_extra", mapping_key=True)
    assert boundary.counts("SELECT 'now', ' NoW ', $$today$$") == {
        "unresolved-temporal-input": 3,
    }
