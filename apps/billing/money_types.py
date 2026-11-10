"""Exact monetary boundaries and immutable posting inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

MAX_MINOR: Final = 2**63 - 1
BASIS_POINTS: Final = 10000
MINIMUM_LINES: Final = 2
SESSION_HASH_LENGTH: Final = 64
MAX_REAIS_TEXT_LENGTH: Final = 64
REAI_TEXT: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)


class JournalInputError(ValueError):
    """Refuse financial input without echoing values."""

    def __init__(self) -> None:
        """Use one fixed non-identifying input failure."""
        super().__init__("invalid exact financial input")


class JournalConflictError(Exception):
    """Refuse conflicting facts, capacity or money-state transitions."""

    def __init__(self) -> None:
        """Use one fixed non-identifying conflict."""
        super().__init__("financial fact or state conflict")


@dataclass(frozen=True, slots=True)
class SourceFact:
    """Manual economic identity; automatic facts use their immutable domain row."""

    kind: str
    id: UUID
    reversal_of: UUID | None = None


@dataclass(frozen=True, slots=True)
class Line:
    """Positive centavos, side and accountable dimensions, never a signed float."""

    account: str
    side: str
    amount_minor: int
    practitioner_id: UUID | None = None
    owner_id: UUID | None = None
    due_on: date | None = None


@dataclass(frozen=True, slots=True)
class PayoutRuleTerms:
    """One versioned per-service practitioner entitlement formula."""

    kind: str
    value: int
    service_type_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ChargeAdjustments:
    """Reductions fixed before any allocation, never PSP fees."""

    discount_minor: int = 0
    payer_adjustment_minor: int = 0


@dataclass(frozen=True, slots=True)
class SettlementObservation:
    """Independently observed money, not a verified or posted collection."""

    amount_minor: int
    kind: str
    account: str
    evidence_reference: UUID


NO_ADJUSTMENTS: Final = ChargeAdjustments()


def minor_amount(value: int, *, zero: bool = False) -> int:
    """Parse an integer-centavo boundary; bool and Decimal are not integers."""
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_MINOR:
        raise JournalInputError
    return value


def round_ratio(numerator: int, denominator: int) -> int:
    """Half-even at policy boundaries, using unbounded integer intermediates."""
    if type(numerator) is not int or type(denominator) is not int:
        raise JournalInputError
    if numerator < 0 or denominator <= 0:
        raise JournalInputError
    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder > denominator or (2 * remainder == denominator and quotient % 2):
        quotient += 1
    return minor_amount(quotient, zero=True)


def round_reais(value: str) -> int:
    """Convert exact decimal text at completion-net-v1's reais input boundary."""
    if (
        type(value) is not str
        or len(value) > MAX_REAIS_TEXT_LENGTH
        or not REAI_TEXT.fullmatch(value)
    ):
        raise JournalInputError
    whole, separator, fraction = value.partition(".")
    scale = 10 ** len(fraction) if separator else 1
    numerator = int(whole) * scale + (int(fraction) if fraction else 0)
    return round_ratio(numerator * 100, scale)


def largest_remainder(whole: int, weights: Sequence[tuple[str, int]]) -> dict[str, int]:
    """Conserve cents; equal remainders prefer lexical stable ids, never row order."""
    minor_amount(whole, zero=True)
    if not weights or len({key for key, _ in weights}) != len(weights):
        raise JournalInputError
    for key, weight in weights:
        if type(key) is not str or not key or type(weight) is not int or weight < 0:
            raise JournalInputError
    denominator = sum(weight for _, weight in weights)
    if denominator == 0:
        raise JournalInputError
    parts = {key: whole * weight // denominator for key, weight in weights}
    priority = sorted(
        weights, key=lambda item: (-(whole * item[1] % denominator), item[0])
    )
    for key, _ in priority[: whole - sum(parts.values())]:
        parts[key] += 1
    return parts
