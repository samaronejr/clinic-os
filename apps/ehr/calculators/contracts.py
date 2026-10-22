"""Pure calculator values; no database, provider, locale or clock dependency."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from typing import Final

NUMBER: Final = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?\Z")
ARITHMETIC_CONTEXT: Final = Context(
    prec=48,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


@dataclass(frozen=True, slots=True)
class Calculation:
    """An explicitly versioned number, never a diagnosis or recommendation."""

    key: str
    version: int
    value: str
    unit: str
    source: str


@dataclass(frozen=True, slots=True)
class Refusal:
    """A closed, content-free refusal; rejected values never enter its text."""

    code: str


@dataclass(frozen=True, slots=True)
class CalculatorInput:
    """Canonical decimal strings or closed choices; units are explicit."""

    key: str
    label: str
    unit: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundaryVector:
    """An inspectable immutable input/result pair for this exact implementation."""

    inputs: tuple[tuple[str, str], ...]
    expected: Calculation | Refusal


@dataclass(frozen=True, slots=True)
class CalculatorDefinition:
    """The implementation and permissions are pinned with the published formula."""

    key: str
    version: int
    title: str
    source: str
    license_status: str
    license_ref: str
    inputs: tuple[CalculatorInput, ...]
    boundary_vectors: tuple[BoundaryVector, ...]


def number(value: str) -> Decimal | None:
    """Reject commas, grouping, exponent notation, whitespace and non-finite input."""
    if not isinstance(value, str) or NUMBER.fullmatch(value) is None:
        return None
    return Decimal(value)
