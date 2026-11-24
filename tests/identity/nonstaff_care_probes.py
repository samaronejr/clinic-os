"""Real care-coordination callables for observed permission delegation decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.ai import care_coordination, care_views
from apps.ai.care_coordination import CareAccessDeniedError
from apps.identity.current_context import CurrentActorError
from django.db import DatabaseError
from django.test import RequestFactory

from identity.nonstaff_differential import DifferentialProbe

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.ai.care_coordination._care_staff",
        "apps.ai.care_views._automation",
        "apps.ai.care_views._care_staff",
        "apps.ai.care_views._clinician",
        "apps.ai.care_views._context",
        "apps.ai.care_views._plans",
    }
)


def care_calls(graph: RbacGraph) -> dict[str, Callable[[], bool]]:
    """Call each real helper whose decision is a delegated permission root."""
    clinic = graph.clinic_a

    def context() -> bool:
        # The page's own response builder, which also lists the actor's plans
        # and their pending bundle references.
        care_views._context(
            RequestFactory().get("/"), clinic, 200, care_views.CareSelection()
        )
        return True

    return {
        "apps.ai.care_coordination._care_staff": lambda: care_coordination._care_staff(
            clinic
        ),
        "apps.ai.care_views._automation": lambda: care_views._automation(clinic),
        "apps.ai.care_views._care_staff": lambda: care_views._care_staff(clinic),
        "apps.ai.care_views._clinician": lambda: care_views._clinician(clinic),
        "apps.ai.care_views._context": context,
        # A human plan alone is not a pending bundle. Exercise the real
        # source-eligible item selection, not just the existence of a plan.
        "apps.ai.care_views._plans": lambda: bool(care_views._plans(clinic)[1]),
    }


def care_probe(symbol: str, invoke: Callable[[], bool]) -> DifferentialProbe:
    """Observe the real callable even when its boundary translates a denial."""

    def call() -> bool:
        try:
            return invoke()
        except (CareAccessDeniedError, CurrentActorError, DatabaseError):
            return False

    return DifferentialProbe(symbol, call, expected=False)


def care_probes(graph: RbacGraph) -> list[DifferentialProbe]:
    """The non-staff census actor is refused by every delegated care helper."""
    calls = care_calls(graph)
    assert calls.keys() == SYMBOLS
    return [care_probe(symbol, calls[symbol]) for symbol in sorted(SYMBOLS)]
