"""Named load profiles and the PF target table (plan section 7).

Targets are proposals until measured; a local run is L1 evidence on a named
profile and never a production SLO claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class UnknownProfileError(ValueError):
    """Reject a profile name outside the registered set."""


class ProfileNotRunnableError(RuntimeError):
    """Refuse a profile that needs a rig this harness does not provide."""


@dataclass(frozen=True, slots=True)
class Target:
    """One PF target: a percentile of a scenario stays within a limit."""

    scenario: str
    limit_ms: int
    source: str


@dataclass(frozen=True, slots=True)
class Profile:
    """One named dataset and load shape."""

    name: str
    description: str
    organizations: int
    clinics_per_org: int
    clinicians_per_clinic: int
    visits_per_clinician_day: int
    patients_per_org: int
    staff_sessions: int
    bulk_import: bool
    samples: int
    runnable_locally: bool = True


PROFILES: Final[dict[str, Profile]] = {
    "P-small": Profile(
        name="P-small",
        description="10 clinics, small patient base; smoke profile",
        organizations=10,
        clinics_per_org=1,
        clinicians_per_clinic=3,
        visits_per_clinician_day=25,
        patients_per_org=200,
        staff_sessions=6,
        bulk_import=False,
        samples=200,
    ),
    "P-100": Profile(
        name="P-100",
        description="100 clinics x 10 clinicians x 25 visits/day = 25,000 visits/day",
        organizations=100,
        clinics_per_org=1,
        clinicians_per_clinic=10,
        visits_per_clinician_day=25,
        patients_per_org=300,
        staff_sessions=20,
        bulk_import=False,
        samples=300,
    ),
    "P-1000": Profile(
        name="P-1000",
        description="1,000 clinics; definition only, needs a multi-node rig",
        organizations=1000,
        clinics_per_org=1,
        clinicians_per_clinic=10,
        visits_per_clinician_day=25,
        patients_per_org=300,
        staff_sessions=200,
        bulk_import=False,
        samples=300,
        runnable_locally=False,
    ),
    "P-large-tenant": Profile(
        name="P-large-tenant",
        description="one tenant with 100,000 patients, 50 staff sessions, bulk import",
        organizations=2,
        clinics_per_org=1,
        clinicians_per_clinic=12,
        visits_per_clinician_day=25,
        patients_per_org=100_000,
        staff_sessions=50,
        bulk_import=True,
        samples=300,
    ),
}

# Plan section 7 PF: latency targets in milliseconds at p95.
TARGETS: Final[tuple[Target, ...]] = (
    Target("staff_navigation", 1500, "PF navigation p95 <= 1.5 s"),
    Target("patient_search_blind_index", 700, "PF patient search p95 <= 700 ms server"),
    Target("agenda_interaction", 700, "PF agenda p95 <= 700 ms server"),
    Target("autosave", 1000, "PF autosave ack p95 <= 1 s"),
    Target("realtime_fanout", 2000, "PF realtime p95 <= 2 s after commit"),
    Target("transcript_partial", 3000, "PF transcript partial p95 <= 3 s"),
    Target("draft_generation", 30_000, "PF final draft p95 <= 30 s after stop"),
)

# PF rows a load harness cannot observe; recorded, never silently passed.
NOT_MEASURED: Final[tuple[tuple[str, str], ...]] = (
    (
        "availability 99.95% monthly SLI",
        "needs a production monthly window; defined as an SLI in "
        "docs/operations/slo.md",
    ),
    (
        "RPO <= 5 min and RTO <= 60 min incl. objects+keys",
        "measured by the restore drill of todo 72, not by load generation",
    ),
    (
        "no acknowledged write lost",
        "asserted by the autosave and restore tests, not a latency target",
    ),
)

FAIRNESS_TOLERANCE: Final = 0.10
DEFAULT_FAIRNESS_TRIALS: Final = 5


def profile_named(name: str) -> Profile:
    """Return one registered profile or raise a stable error."""
    profile = PROFILES.get(name)
    if profile is None:
        message = f"unknown profile; choose one of {sorted(PROFILES)}"
        raise UnknownProfileError(message)
    return profile


def target_for(scenario: str) -> Target | None:
    """Return the PF target for a scenario, or None when informational."""
    return next((item for item in TARGETS if item.scenario == scenario), None)
