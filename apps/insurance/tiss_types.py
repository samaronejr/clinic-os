"""Immutable transport facts; clinical and financial assertions retain their sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class Professional:
    """Reviewed billing identity, not a provider-created signing identity."""

    name: str
    council: str
    number: str
    uf: str
    cbo: str


@dataclass(frozen=True, slots=True)
class Procedure:
    """One source-backed service mapped to an imported TUSS term."""

    table: str
    code: str
    description: str
    quantity: int
    unit_minor: int


@dataclass(frozen=True, slots=True)
class GuideFacts:
    """The explicit clinical facts never change when a payer denies payment."""

    number: str
    kind: str
    registry: str
    card: str
    provider_code: str
    provider_name: str
    cnes: str
    performed_on: date
    professional: Professional
    procedures: tuple[Procedure, ...]
    principal_number: str = ""
    newborn: str = "N"
    character: str = "1"
    attendance: str = "04"
    accident: str = "9"
    regime: str = "01"
    consultation: str = "1"


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """One homogeneous payer batch with a deterministic transaction timestamp."""

    number: str
    registered_at: datetime
    guides: tuple[GuideFacts, ...]


@dataclass(frozen=True, slots=True)
class Adjudication:
    """A return is an adjudication fact, never evidence that cash arrived."""

    guide_number: str
    sequence: int
    informed_minor: int
    accepted_minor: int
    denied_minor: int
    glosas: tuple[tuple[str, int], ...]
    registry: str = ""
    batch_number: str = ""
    card: str = ""
    table: str = ""
    code: str = ""
    quantity: int = 0
