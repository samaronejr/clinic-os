"""Spreadsheet-safe CSV output: no exported cell can start a formula.

Spreadsheet applications evaluate a cell whose first character is ``=``,
``+``, ``-`` or ``@`` (and some treat a leading tab or carriage return the
same way), so attacker-controlled source values such as
``=HYPERLINK("https://example.invalid","x")`` must never reach an export as
live formulas. Every exported cell passes through ``neutralize_cell``, which
prefixes such cells with an apostrophe (the OWASP CSV-injection guidance);
leading whitespace and full-width forms do not bypass the check.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

FORMULA_TRIGGERS: Final = frozenset("=+-@\t\r\uff1d\uff0b\uff0d\uff20")


def neutralize_cell(value: str) -> str:
    """Return the cell unchanged unless its first visible character is a trigger."""
    if not isinstance(value, str):
        message = "spreadsheet cells must be text"
        raise TypeError(message)
    stripped = value.lstrip(" \u00a0")
    if value[:1] in FORMULA_TRIGGERS or stripped[:1] in FORMULA_TRIGGERS:
        return "'" + value
    return value


def is_formula_like(value: str) -> bool:
    """Whether a source cell would be evaluated by a spreadsheet application."""
    return neutralize_cell(value) != value


def write_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> bytes:
    """Write UTF-8 CSV with every cell (header included) neutralized."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([neutralize_cell(cell) for cell in header])
    for row in rows:
        writer.writerow([neutralize_cell(cell) for cell in row])
    return stream.getvalue().encode("utf-8")
