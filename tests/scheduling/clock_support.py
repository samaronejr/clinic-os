"""Complete test-only SQL clock control, retaining an explicit ambient fallback."""

from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Final

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

# Detect every PostgreSQL spelling, even ones not currently used. Exact source
# and live-catalog assertions require review when another time read is added.
SQL_TIME: Final = re.compile(
    r"\b(?:pg_catalog\.)?(?:"
    r"(?:statement_timestamp|clock_timestamp|now)\s*\(\s*\)|"
    r"current_date\b|(?:current_timestamp|localtimestamp)\b(?:\s*\(\s*\d+\s*\))?)",
    re.IGNORECASE,
)
SQL_CLOCKS: Final = {
    "scheduling_appointment_guard_v1()": {"current_timestamp": 1},
    "patient_booking_scope()": {"statement_timestamp()": 2},
    "patient_booking_slots(date,uuid)": {"statement_timestamp()": 1},
    "patient_booking_receipt()": {"statement_timestamp()": 1},
    "scheduling_definition_guard()": {"statement_timestamp()": 3},
    "scheduling_generated_block_guard()": {"statement_timestamp()": 1},
}
SOURCE_CLOCKS: Final = {
    "_appointment_sql.py": {"current_timestamp": 1},
    "_patient_booking_sql.py": {"statement_timestamp()": 4},
    "_resource_slots_sql.py": {"statement_timestamp()": 1},
    "_resources_sql.py": {"statement_timestamp()": 4},
}
AUTHORITY_CLOCKS: Final = frozenset({"patient_booking_scope()"})


def clock_counts(source: str) -> Counter[str]:
    return Counter(
        re.sub(r"\s+", "", match.group()).lower().removeprefix("pg_catalog.")
        for match in SQL_TIME.finditer(source)
    )


@contextmanager
def frozen_sql_clocks(
    database_url: str, reference: datetime, ambient: datetime | None = None
) -> Iterator[datetime]:
    """Control every audited read; auth expiry uses a separate DB-time snapshot.

    COALESCE keeps the ambient expression observable for adversarial probes: even
    advancing that fallback past a booking cannot escape the explicit override.
    No production function or predicate is changed outside this fixture scope.
    """
    originals: list[tuple[str, str]] = []
    with psycopg.connect(database_url, autocommit=True) as owner:
        row = owner.execute("SELECT statement_timestamp()").fetchone()
        assert row is not None
        authority_now = row[0]
        assert isinstance(authority_now, datetime)
        try:
            for signature, expected in SQL_CLOCKS.items():
                row = owner.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)",
                    [f"clinic_app.{signature}"],
                ).fetchone()
                assert row is not None
                original = str(row[0])
                assert clock_counts(original) == expected, signature
                controlled = sql.Literal(
                    authority_now if signature in AUTHORITY_CLOCKS else reference
                ).as_string(owner)

                def replacement(
                    match: re.Match[str], controlled: str = controlled
                ) -> str:
                    fallback = match.group()
                    if ambient is not None:
                        advanced = sql.Literal(ambient).as_string(owner)
                        fallback = f"COALESCE({advanced}, {fallback})"
                    return f"COALESCE({controlled}, {fallback})"

                owner.execute(SQL_TIME.sub(replacement, original).encode())
                originals.append((signature, original))
            yield authority_now
        finally:
            for signature, original in reversed(originals):
                owner.execute(original.encode())
                assert owner.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)",
                    [f"clinic_app.{signature}"],
                ).fetchone() == (original,)
