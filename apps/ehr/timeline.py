"""Query-time authorization precedes every timeline filter, page and count."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.models import Episode
from apps.ehr.services import ClinicalAccessDeniedError, decided
from apps.ehr.timeline_models import TimelineProjection

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

MAX_PAGE_SIZE: Final = 100
MAX_PAGE_OFFSET: Final = 10000
TIMELINE_KINDS: Final = ("observation", "medication", "note", "encounter", "addendum")


@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineFilters:
    """Bounded typed selectors; clinical text is never a query predicate."""

    kind: str = ""
    start: datetime | None = None
    end: datetime | None = None
    episode_id: UUID | None = None
    offset: int = 0
    limit: int = 50

    def __post_init__(self) -> None:
        """Reject ambiguous dates and unbounded selectors at the value boundary."""
        if (
            self.kind not in ("", *TIMELINE_KINDS)
            or not 1 <= self.limit <= MAX_PAGE_SIZE
            or not 0 <= self.offset <= MAX_PAGE_OFFSET
            or (self.start is not None and timezone.is_naive(self.start))
            or (self.end is not None and timezone.is_naive(self.end))
            or (
                self.start is not None
                and self.end is not None
                and self.start >= self.end
            )
        ):
            raise ValidationError(_("Choose a valid timeline period and type."))


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """The total counts only the rows current source policies admit."""

    entries: tuple[TimelineProjection, ...]
    total: int
    has_more: bool


def timeline(
    *, clinic_id: UUID, enrollment_id: UUID, filters: TimelineFilters
) -> TimelinePage:
    """Materialize only current-authorized source metadata in the exact clinic."""
    authority = partial(
        patient_scope, clinic_id=clinic_id, enrollment_id=enrollment_id, action="read"
    )
    scope = authority()
    if (
        filters.episode_id is not None
        and not Episode.objects.filter(
            pk=filters.episode_id, clinic_id=clinic_id, patient_id=scope.patient_id
        ).exists()
    ):
        raise ClinicalAccessDeniedError
    with decided(authority):
        rows = TimelineProjection.objects.filter(
            clinic_id=clinic_id, patient_id=scope.patient_id
        )
        if filters.kind:
            rows = rows.filter(kind=filters.kind)
        if filters.start is not None:
            rows = rows.filter(effective_at__gte=filters.start)
        if filters.end is not None:
            rows = rows.filter(effective_at__lt=filters.end)
        if filters.episode_id is not None:
            rows = rows.filter(episode_id=filters.episode_id)
        total = rows.count()
        entries = tuple(
            rows.order_by("-effective_at", "-pk")[
                filters.offset : filters.offset + filters.limit
            ]
        )
        record_phase1_event(
            "ehr.timeline.viewed", clinic_id=clinic_id, affected_record_id=enrollment_id
        )
        return TimelinePage(entries, total, filters.offset + len(entries) < total)
