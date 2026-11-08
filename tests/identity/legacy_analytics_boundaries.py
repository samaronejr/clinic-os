"""Real report authority boundaries in the legacy role parity census."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from apps.analytics.contracts import Query
from apps.analytics.services import authorize, refresh_metric

from identity.legacy_parity_support import ADMINS, Boundary

BOUNDARIES = (
    Boundary(
        "apps.analytics.services.authorize",
        "analytics",
        ADMINS,
        lambda world, valid: authorize(clinic_id=world.clinic_for(valid)),
    ),
    Boundary(
        "apps.analytics.services.refresh_metric",
        "analytics",
        ADMINS,
        lambda world, valid: refresh_metric(
            clinic_id=world.clinic_for(valid),
            query=Query(
                "no_show",
                datetime(2035, 6, 1, tzinfo=UTC),
                datetime(2035, 6, 2, tzinfo=UTC),
                "America/Sao_Paulo",
            ),
            command_id=uuid4(),
        ),
    ),
)
