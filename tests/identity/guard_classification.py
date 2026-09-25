"""Inventory labels cannot exempt an implementation that reads staff state."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from identity.staff_state_analysis import StaffAnalysis


class Candidate(TypedDict):
    symbol: str
    signals: list[str]
    kind: str
    probes: NotRequired[list[str]]
    enforced_by: NotRequired[list[str]]
    reason: NotRequired[str]


def assert_staff_coverage(
    candidates: list[Candidate],
    analysis: StaffAnalysis,
    executable: set[str],
) -> None:
    """Require actual executable authority for every detected staff dependency.

    This check never trusts a row's signals or free-text explanation. Delegation
    is accepted only to a reachable, staff-reading, executable boundary. Direct
    queries cannot be relabeled as nonstaff, presentation or infrastructure.
    """
    for row in candidates:
        symbol = row["symbol"]
        assert symbol in analysis.direct, (symbol, "source unavailable")
        reads = analysis.evidence(symbol)
        if not reads:
            continue
        assert row["kind"] in {"direct", "polymorphic", "delegated"}, (
            symbol,
            "staff-state read requires executable authority",
            sorted(reads),
        )
        if row["kind"] == "delegated":
            assert "enforced_by" in row, symbol
            targets = set(row["enforced_by"])
            assert targets, symbol
            assert targets <= analysis.reachable(symbol), (
                symbol,
                "unresolved delegation",
                targets,
            )
            assert targets <= executable, (symbol, "missing executable oracle", targets)
            assert any(analysis.evidence(target) for target in targets), (
                symbol,
                "delegation does not exercise staff authority",
            )
        else:
            assert "probes" in row, symbol
            assert row["probes"], symbol
            assert set(row["probes"]) <= executable, (
                symbol,
                "missing executable oracle",
            )
