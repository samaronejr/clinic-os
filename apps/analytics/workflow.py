"""One local, idempotent refresh handler on the existing fenced workflow engine."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from apps.analytics.contracts import AnalyticsAccessDeniedError, Query
from apps.analytics.services import authorize, refresh_metric
from apps.identity.models import Clinic
from apps.workflows.access import WorkflowAccessDeniedError

if TYPE_CHECKING:
    from datetime import datetime

    from apps.workflows.models import WorkflowStep


def refresh_step(step: WorkflowStep, now: datetime) -> Literal["completed"]:
    """Pin the window to run creation, so retry never changes a command's payload."""
    del now
    try:
        authorize(clinic_id=step.clinic_id)
    except AnalyticsAccessDeniedError as error:
        raise WorkflowAccessDeniedError from error
    specification = step.run.definition_version.steps[step.position]
    zone = str(Clinic.objects.get(pk=step.clinic_id).timezone)
    end = step.run.created_at
    refresh_metric(
        clinic_id=step.clinic_id,
        query=Query(
            specification["metric_id"],
            end - timedelta(days=specification["days"]),
            end,
            zone,
        ),
        command_id=step.pk,
    )
    return "completed"
