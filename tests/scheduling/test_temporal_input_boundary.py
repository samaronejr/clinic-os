"""Pin catalog-derived literal coercion risk, not a list of magic input words."""

import pytest

from scheduling.clock_catalog import catalog_readers
from scheduling.clock_temporal_inputs import LiteralInputs

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("source", "risky"),
    [
        ("SELECT 'not temporal'::text", False),
        ("DECLARE x text := 'now'; BEGIN RETURN x; END", False),
        ("SELECT '2001-01-01'::timestamptz", True),
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
    assert LiteralInputs(catalog_readers()).unresolved(source) is risky
