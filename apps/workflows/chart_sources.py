"""Patient-bound task excerpts; the task policy remains the row authority."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.db.models import Q

from apps.core.chart_sources import CHART_LIMIT, ChartBatch, ChartSource
from apps.identity.current_context import require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.models import Appointment
from apps.workflows.models import Task
from apps.workflows.validation import reference

if TYPE_CHECKING:
    from uuid import UUID


def read_task_sources(*, clinic_id: UUID, enrollment_id: UUID) -> ChartBatch:
    """Narrow to this patient before bounding; never expose clinic-wide counts."""
    require_permission(
        "clinical.read", clinic_id=clinic_id, patient_enrollment_id=enrollment_id
    )
    require_permission("tasks.view", clinic_id=clinic_id)
    ref = reference({"kind": "enrollment", "id": str(enrollment_id)})
    enrollment = PatientClinicEnrollment.objects.get(
        pk=enrollment_id, clinic_id=clinic_id
    )
    appointments = Appointment.objects.filter(
        clinic_id=clinic_id, patient_id=enrollment.patient_id
    ).values_list("pk", flat=True)
    rows = tuple(
        Task.objects.filter(
            Q(subject_ref=ref)
            | Q(
                subject_ref__kind="appointment",
                subject_ref__id__in=[str(identifier) for identifier in appointments],
            ),
            clinic_id=clinic_id,
            state__in=("open", "assigned", "in_progress"),
        ).order_by("due_at", "pk")[: CHART_LIMIT + 1]
    )
    return ChartBatch(
        tuple(
            ChartSource(
                "outstanding_tasks",
                "task",
                task.pk,
                task.revision,
                task.updated_at,
                json.dumps(
                    {
                        "kind": task.kind,
                        "state": task.state,
                        "priority": task.priority,
                        "due_at": task.due_at.isoformat(),
                    },
                    sort_keys=True,
                ),
            )
            for task in rows[:CHART_LIMIT]
        ),
        (("outstanding_tasks", "limited" if len(rows) > CHART_LIMIT else "complete"),),
    )
