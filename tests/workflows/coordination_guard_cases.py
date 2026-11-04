"""Seeded guard cases for release coordination and the urgent queue (todo 55).

The public pages decide their navigation permission before the auth decorators
and answer refusals with the shared 403 page (transport cases); the decorated
inner pages and the services raise the actor error themselves. Only the release
needs committed records (``coordination_world``): an acknowledged version the
actor may release and the clinic's current notice.
"""

from __future__ import annotations

import inspect
from datetime import time
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.workflows import coordination_views, release, urgent

from workflows.coordination_world import NOTICE
from workflows.guard_cases import ALLOWED, GuardCase, GuardWorld, _remember

if TYPE_CHECKING:
    from collections.abc import Mapping

    from workflows.guard_cases import Outcome

COORDINATION_SEEDED = frozenset({"apps.workflows.release.release_result_to_patient"})
VIEWS = "apps.workflows.coordination_views."


# The page layout's navigation also asks demographics.read; without it the
# settings pages still decide by their own permission alone.
NAVIGATION_ONLY: Mapping[str, tuple[Outcome, None]] = {
    "demographics.read": (ALLOWED, None)
}


def _page(
    name: str,
    permission: str,
    *,
    narrowed: Mapping[str, tuple[Outcome, None]] | None = None,
) -> tuple[GuardCase, GuardCase]:
    public = getattr(coordination_views, name)
    inner = getattr(coordination_views, "_" + name.removesuffix("_page"))
    return (
        GuardCase(
            VIEWS + name,
            permission,
            lambda w: _remember(w.request, lambda request: public(request, w.clinic)),
            transport=True,
            narrowed=narrowed or {},
        ),
        GuardCase(
            VIEWS + inner.__name__,
            permission,
            lambda w: inspect.unwrap(inner)(w.request, w.clinic),
            narrowed=narrowed or {},
        ),
    )


def _release(w: GuardWorld) -> object:
    return release.release_result_to_patient(
        clinic_id=w.clinic,
        result_version_id=w.coordination.version,
        template_version=w.coordination.notice_version,
        audience="patient",
    )


def _coverage(w: GuardWorld) -> object:
    return urgent.set_urgent_coverage(
        clinic_id=w.clinic,
        expected_version=0,
        weekdays=(0, 1, 2, 3, 4),
        opens_at=time(8, 0),
        closes_at=time(18, 0),
        response_hours=4,
        responsible_roles=("nurse",),
    )


def coordination_cases() -> tuple[GuardCase, ...]:
    return (
        *_page("urgent_concerns_page", "demographics.read"),
        *_page(
            "urgent_coverage_page", "configuration.clinic", narrowed=NAVIGATION_ONLY
        ),
        *_page(
            "release_notices_page", "configuration.propose", narrowed=NAVIGATION_ONLY
        ),
        GuardCase(
            "apps.workflows.release.current_notice",
            "configuration.propose",
            lambda w: release.current_notice(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.release.publish_release_notice",
            "configuration.propose",
            lambda w: release.publish_release_notice(clinic_id=w.clinic, body=NOTICE),
        ),
        GuardCase(
            "apps.workflows.release.release_deliveries",
            "configuration.propose",
            lambda w: release.release_deliveries(
                clinic_id=w.clinic, result_version_id=uuid4()
            ),
        ),
        GuardCase(
            "apps.workflows.release.release_result_to_patient",
            "configuration.propose",
            _release,
        ),
        GuardCase(
            "apps.workflows.urgent.current_coverage",
            "demographics.read",
            lambda w: urgent.current_coverage(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.urgent.coverage_settings",
            "configuration.clinic",
            lambda w: urgent.coverage_settings(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.urgent.set_urgent_coverage",
            "configuration.clinic",
            _coverage,
        ),
    )
