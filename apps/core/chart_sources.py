"""Ephemeral, source-exact chart excerpts shared by the owning domains."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

type ChartSection = Literal[
    "chronology",
    "active_problems",
    "allergies",
    "medications",
    "observations",
    "recent_results",
    "outstanding_tasks",
    "prior_decisions",
]
type CoverageState = Literal["complete", "limited", "unavailable"]
type Evidence = Literal["recorded", "documented_none"]

SECTIONS: tuple[ChartSection, ...] = (
    "chronology",
    "active_problems",
    "allergies",
    "medications",
    "observations",
    "recent_results",
    "outstanding_tasks",
    "prior_decisions",
)
CHART_LIMIT = 25


@dataclass(frozen=True, slots=True)
class ChartSource:
    """One assertion has one exact source, never an inferred clinical finding."""

    section: ChartSection
    kind: str
    id: UUID
    version: int
    date: datetime
    text: str = field(repr=False)
    evidence: Evidence = "recorded"

    def reference(self) -> dict[str, str | int]:
        """Bind the excerpt as well as the retained version and date."""
        return {
            "section": self.section,
            "kind": self.kind,
            "id": str(self.id),
            "version": self.version,
            "date": self.date.isoformat(),
            "digest": sha256(self.text.encode("utf-8")).hexdigest(),
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ChartBatch:
    """Coverage describes available records only, never concealed source counts."""

    sources: tuple[ChartSource, ...]
    coverage: tuple[tuple[ChartSection, CoverageState], ...]
