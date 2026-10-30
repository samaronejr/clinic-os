"""A patient-scoped context assembler with no retrieval engine or direct chart ORM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.ai.brief_contracts import parse_request
from apps.ai.contracts import GatewayAccessDeniedError
from apps.core.chart_sources import ChartBatch, ChartSource
from apps.ehr.chart_sources import chart_enrollment, read_chart
from apps.identity.current_context import CurrentActorError
from apps.orders.chart_sources import read_result_sources
from apps.orders.values import OrdersAccessDeniedError
from apps.workflows.chart_sources import read_task_sources

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChartContext:
    """Only owning-domain services may supply sources and coverage."""

    enrollment_id: UUID
    chart: ChartBatch

    @property
    def sources(self) -> tuple[ChartSource, ...]:
        """Keep plaintext ephemeral, excluded from gateway input references."""
        return self.chart.sources


def assemble_context(
    *,
    clinic_id: UUID,
    enrollment_id: UUID | None = None,
    encounter_id: UUID | None = None,
) -> ChartContext:
    """Decide patient authority before any source read, count or coverage report."""
    enrolled = chart_enrollment(
        clinic_id=clinic_id, enrollment_id=enrollment_id, encounter_id=encounter_id
    )
    clinical = read_chart(clinic_id=clinic_id, enrollment_id=enrolled)
    try:
        results = read_result_sources(clinic_id=clinic_id, enrollment_id=enrolled)
    except (CurrentActorError, OrdersAccessDeniedError):
        results = ChartBatch((), (("recent_results", "unavailable"),))
    try:
        tasks = read_task_sources(clinic_id=clinic_id, enrollment_id=enrolled)
    except CurrentActorError:
        tasks = ChartBatch((), (("outstanding_tasks", "unavailable"),))
    return ChartContext(
        enrolled,
        ChartBatch(
            clinical.sources + results.sources + tasks.sources,
            clinical.coverage + results.coverage + tasks.coverage,
        ),
    )


def recheck_context(*, clinic_id: UUID, content: str) -> None:
    """Queued references acquire no right to a source revoked before dispatch."""
    request = parse_request(content)
    current = assemble_context(clinic_id=clinic_id, enrollment_id=request.enrollment)
    permitted = [source.reference() for source in current.sources]
    if any(reference not in permitted for reference in request.sources):
        raise GatewayAccessDeniedError
