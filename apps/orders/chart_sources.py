"""Source-exact result excerpts, routed through the current matched-order reader."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.core.chart_sources import CHART_LIMIT, ChartBatch, ChartSource
from apps.orders.queries import result_rows

if TYPE_CHECKING:
    from uuid import UUID


def read_result_sources(*, clinic_id: UUID, enrollment_id: UUID) -> ChartBatch:
    """Preserve exact values and provenance; ingestion never implies review."""
    rows = result_rows(clinic_id=clinic_id, enrollment_id=enrollment_id)
    sources = tuple(
        ChartSource(
            "recent_results",
            "result",
            row.match.version_id,
            row.match.version.number,
            row.match.version.created_at,
            json.dumps(
                {
                    "observations": [
                        observation.source
                        for observation in row.match.version.observations.order_by(
                            "position"
                        )
                    ],
                    "acknowledged": row.acknowledged,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for row in rows[:CHART_LIMIT]
    )
    return ChartBatch(
        sources,
        (("recent_results", "limited" if len(rows) > CHART_LIMIT else "complete"),),
    )
