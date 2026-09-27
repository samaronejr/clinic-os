"""Permission and owner boundaries shared by every task and workflow command."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.db import connection

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.models import Appointment
from apps.workflows.models import Task
from apps.workflows.validation import reference

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.workflows.validation import TaskOwner


class WorkflowAccessDeniedError(CurrentActorError):
    """Use one payload-free denial for every inaccessible workflow record."""

    def __init__(self) -> None:
        """Do not reflect identifiers or distinguish unknown from foreign records."""
        super().__init__("current actor unauthorized")


def require_clinic_access(*, clinic_id: UUID, permission: str) -> tuple[Clinic, UUID]:
    """Derive the actor from GUCs and reject unknown/foreign clinics identically."""
    actor = require_permission(permission, clinic_id=clinic_id)
    return Clinic.objects.get(pk=clinic_id), actor


def require_manager(*, clinic_id: UUID) -> UUID:
    """Reassignment authority also owns definition publication and run control."""
    return require_permission("tasks.reassign", clinic_id=clinic_id)


def require_task_access(
    *, clinic_id: UUID, task_id: UUID, permission: str, owned: bool = False
) -> Task:
    """Lock a visible task; completing work never borrows a manager's ownership."""
    require_clinic_access(clinic_id=clinic_id, permission=permission)
    task = (
        Task.objects.select_for_update().filter(pk=task_id, clinic_id=clinic_id).first()
    )
    if task is None or (owned and not owner_matches(task)):
        raise WorkflowAccessDeniedError
    return task


def owner_matches(task: Task) -> bool:
    """Require current permission before checking exact-clinic ownership."""
    actor = require_permission("tasks.view", clinic_id=task.clinic_id)
    return task.owner_user_id == actor or (
        bool(task.owner_role)
        and UserClinicRole.objects.filter(
            clinic_id=task.clinic_id, user_id=actor, role=task.owner_role
        ).exists()
    )


def may_self_claim(task: Task, owner: TaskOwner) -> bool:
    """Staff may take their own open task or work they own; other moves need a manager.

    The decision reads the relation itself instead of relying on row visibility,
    so it holds for any record a caller has already loaded.
    """
    actor = require_permission("tasks.assign", clinic_id=task.clinic_id)
    return (
        owner.user_id is not None
        and owner.user_id == actor
        and (
            (task.state == "open" and task.created_by_id == actor)
            or owner_matches(task)
        )
    )


def require_owner_target(*, clinic_id: UUID, user_id: UUID | None, role: str) -> None:
    """Use a narrow resolver: runtime cannot SELECT the identity user table."""
    require_permission("tasks.assign", clinic_id=clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_owner_valid(%s,%s,%s)",
            [clinic_id, user_id, role],
        )
        if cursor.fetchone() != (True,):
            raise WorkflowAccessDeniedError


def require_reference(
    *, clinic_id: UUID, value: Mapping[str, object]
) -> dict[str, str]:
    """Validate shape and same-clinic existence without loading clinical content."""
    require_permission("tasks.view", clinic_id=clinic_id)
    ref = reference(dict(value))
    identifier = UUID(ref["id"])
    if ref["kind"] == "clinic":
        valid = identifier == clinic_id
    elif ref["kind"] == "enrollment":
        valid = PatientClinicEnrollment.objects.filter(
            pk=identifier, clinic_id=clinic_id
        ).exists()
    elif ref["kind"] == "appointment":
        valid = Appointment.objects.filter(pk=identifier, clinic_id=clinic_id).exists()
    else:
        valid = Task.objects.filter(pk=identifier, clinic_id=clinic_id).exists()
    if not valid:
        raise WorkflowAccessDeniedError
    return ref
