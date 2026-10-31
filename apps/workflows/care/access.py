"""Authority and record lookup for care records (SC-1).

Unknown, invisible and wrong-clinic records share one payload-free denial. The
actor always comes from the transaction GUCs; no service takes an acting user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, cast
from uuid import UUID, uuid5

from django.db import connection

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.models import Clinic
from apps.tenancy.envelope import blind_indexes
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.models import CareAction, CarePlan
from apps.workflows.models import Task
from apps.workflows.validation import WorkflowConflictError, canonical

if TYPE_CHECKING:
    from collections.abc import Sequence


COMMAND_PURPOSE: Final = "workflows.care.command.v1"


class CommandRow(Protocol):
    """A revisioned row that records the command which last changed it."""

    pk: UUID
    clinic_id: UUID
    revision: int
    last_command_key: UUID | None
    last_command_digest: str

    def save(self, *, update_fields: Sequence[str]) -> None:
        """Persist exactly the named columns."""


def require_staff(*, clinic_id: UUID, permission: str = "tasks.assign") -> Clinic:
    """Require a current staff permission and return the exact clinic."""
    require_permission(permission, clinic_id=clinic_id)
    return Clinic.objects.get(pk=clinic_id)


def require_clinician(*, clinic_id: UUID, enrollment_id: UUID) -> UUID:
    """Require patient-scoped clinical authority for this exact enrollment."""
    return require_permission(
        "clinical.write", clinic_id=clinic_id, patient_enrollment_id=enrollment_id
    )


def require_observer(*, clinic_id: UUID, enrollment_id: UUID) -> UUID:
    """Require the patient's nurse (observations) or clinician (clinical writes).

    A patient-reported answer is clinical data: routing authority such as
    ``tasks.reassign`` never records one (RP matrix, manager: none).
    """
    try:
        return require_permission(
            "observation.write",
            clinic_id=clinic_id,
            patient_enrollment_id=enrollment_id,
        )
    except CurrentActorError:
        return require_clinician(clinic_id=clinic_id, enrollment_id=enrollment_id)


def load_plan(*, clinic_id: UUID, plan_id: UUID) -> CarePlan:
    """Lock a visible plan; absence and invisibility are the same denial."""
    require_permission("tasks.view", clinic_id=clinic_id)
    plan = (
        CarePlan.objects.select_for_update()
        .filter(pk=plan_id, clinic_id=clinic_id)
        .first()
    )
    if plan is None:
        raise WorkflowAccessDeniedError
    return plan


def load_action(
    *, clinic_id: UUID, action_id: UUID, permission: str
) -> tuple[CareAction, Task | None]:
    """Lock a visible action and its task, task first, after the permission check.

    Every task path locks the task before its action, so a care command and a
    concurrent command from the task workspace never wait on each other in
    opposite order. The composite key keeps the task in the action's clinic.
    """
    require_permission(permission, clinic_id=clinic_id)
    found = CareAction.objects.filter(pk=action_id, clinic_id=clinic_id).first()
    if found is None:
        raise WorkflowAccessDeniedError
    task = None
    if found.task_id is not None:
        task = Task.objects.select_for_update().filter(pk=found.task_id).first()
        if task is None:
            raise WorkflowAccessDeniedError
    action = CareAction.objects.select_for_update().filter(pk=found.pk).first()
    if action is None:
        raise WorkflowAccessDeniedError
    return action, task


def plan_of(action: CareAction) -> CarePlan:
    """Return the action's plan; an invisible plan is the shared denial."""
    plan = CarePlan.objects.filter(pk=action.plan_id).first()
    if plan is None:
        raise WorkflowAccessDeniedError
    return plan


def version_scope(*, clinic_id: UUID, version_id: UUID) -> tuple[UUID, UUID, str]:
    """Return (encounter, enrollment, state) of a note version in this clinic.

    An identity-only resolver, so the clinician check can precede any row the
    caller's RLS would hide. Unknown and foreign versions share one denial.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT encounter_id,enrollment_id,state "
            "FROM clinic_app.workflows_care_version_scope(%s,%s)",
            [clinic_id, version_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise WorkflowAccessDeniedError
    return row[0], row[1], str(row[2])


def occurrence_enrollment(*, clinic_id: UUID, occurrence_id: UUID) -> UUID:
    """Return a PRO slot's enrollment by identity only, so authority comes first.

    The slot itself is visible only to the patient's clinicians; the resolver
    lets the patient-scoped check precede that lookup. Unknown, foreign and
    invisible slots share one denial.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_care_occurrence_scope(%s,%s)",
            [clinic_id, occurrence_id],
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise WorkflowAccessDeniedError
    return cast("UUID", row[0])


def require_current_source(action: CareAction) -> None:
    """Refuse work whose source version is no longer the finalized one.

    A clinician's confirmation reopens a flagged action. The same rule is
    enforced by the database guard, so a scanner delay never opens a window.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_care_source_current(%s)", [action.pk]
        )
        if cursor.fetchone() != (True,):
            raise WorkflowConflictError


def keyed_digests(terms: object) -> tuple[str, ...]:
    """Bind canonical terms under the tenant's data keys, one digest per version.

    Care terms can carry encrypted content from small spaces (PHQ-9 answers,
    instruction text, referral targets) that a plain hash would reveal offline.
    Writes store the newest digest, the last; a replay matches any version, so
    a key rotation never turns an identical retry into a conflict.
    """
    return tuple(
        index.hex()
        for index in blind_indexes(purpose=COMMAND_PURPOSE, plaintext=canonical(terms))
    )


def bind_command(row: CommandRow, expected: int, verb: str, terms: object) -> bool:
    """Compare-and-set on the revision; an identical retry changes nothing.

    The key derives from row, verb and the revision the caller saw, and the
    keyed digest binds the actor and terms, so a different replay is a conflict.
    """
    key = uuid5(row.pk, f"{verb}:{expected}")
    digests = keyed_digests({"actor": str(current_actor_id()), "terms": terms})
    if row.last_command_key == key:
        if row.last_command_digest not in digests:
            raise WorkflowConflictError
        return False
    if type(expected) is not int or row.revision != expected:
        raise WorkflowConflictError
    row.last_command_key, row.last_command_digest = key, digests[-1]
    return True


def save_command(row: CommandRow, fields: tuple[str, ...], event: str) -> None:
    """Persist one bound transition and append its registered audit event."""
    row.revision += 1
    row.save(
        update_fields=(
            *fields,
            "revision",
            "last_command_key",
            "last_command_digest",
            "updated_at",
        )
    )
    record_phase1_event(event, clinic_id=row.clinic_id, affected_record_id=row.pk)
