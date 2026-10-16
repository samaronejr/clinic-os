"""Draft, validation, publication and retirement of pinned workflow versions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_clinic_access,
    require_manager,
)
from apps.workflows.models import WorkflowDefinitionVersion
from apps.workflows.validation import (
    WorkflowConflictError,
    digest,
    machine_key,
    validate_steps,
)

if TYPE_CHECKING:
    from uuid import UUID


@transaction.atomic
def create_definition(
    *, clinic_id: UUID, key: str, steps: object, idempotency_key: UUID
) -> WorkflowDefinitionVersion:
    """Allocate a draft version, never mutate an existing published version."""
    actor = require_manager(clinic_id=clinic_id)
    clinic, _actor = require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    machine_key(key)
    checked = validate_steps(steps)
    fingerprint = digest({"actor": str(actor), "key": key, "steps": checked})
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
            [f"clinic-workflow-definition-v1:{clinic_id}:command:{idempotency_key}"],
        )
    previous = WorkflowDefinitionVersion.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if previous is not None:
        if (
            previous.creation_fingerprint != fingerprint
            or previous.created_by_id != actor
        ):
            raise WorkflowConflictError
        return previous
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
            [f"clinic-workflow-definition-v1:{clinic_id}:{key}"],
        )
    latest = WorkflowDefinitionVersion.objects.filter(
        clinic_id=clinic_id, key=key
    ).aggregate(latest=Max("version"))["latest"]
    definition = WorkflowDefinitionVersion.objects.create(
        organization_id=clinic.organization_id,
        clinic=clinic,
        key=key,
        version=(latest or 0) + 1,
        steps=checked,
        created_by_id=actor,
        state=WorkflowDefinitionVersion.State.DRAFT,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        creation_fingerprint=fingerprint,
    )
    definition.refresh_from_db()
    record_phase1_event(
        "workflows.definition.created",
        clinic_id=clinic_id,
        affected_record_id=definition.pk,
    )
    return definition


def _definition(
    *, clinic_id: UUID, definition_version_id: UUID
) -> WorkflowDefinitionVersion:
    require_manager(clinic_id=clinic_id)
    definition = (
        WorkflowDefinitionVersion.objects.select_for_update()
        .filter(pk=definition_version_id, clinic_id=clinic_id)
        .first()
    )
    if definition is None:
        raise WorkflowAccessDeniedError
    return definition


def _command(
    *,
    definition: WorkflowDefinitionVersion,
    action: str,
    expected_revision: int,
    idempotency_key: UUID,
    terms: object,
) -> bool:
    actor = require_manager(clinic_id=definition.clinic_id)
    command_digest = digest(
        {
            "actor": str(actor),
            "action": action,
            "revision": expected_revision,
            "terms": terms,
        }
    )
    if definition.last_command_key == idempotency_key:
        if definition.last_command_digest != command_digest:
            raise WorkflowConflictError
        return False
    if definition.revision != expected_revision:
        raise WorkflowConflictError
    definition.revision += 1
    definition.last_command_key = idempotency_key
    definition.last_command_digest = command_digest
    return True


def _save(definition: WorkflowDefinitionVersion, event: str) -> None:
    definition.save(
        update_fields=(
            "state",
            "steps",
            "fingerprint",
            "validated_at",
            "published_by",
            "published_at",
            "retired_at",
            "revision",
            "last_command_key",
            "last_command_digest",
        )
    )
    definition.refresh_from_db()
    record_phase1_event(
        event, clinic_id=definition.clinic_id, affected_record_id=definition.pk
    )


@transaction.atomic
def revise_definition(
    *,
    clinic_id: UUID,
    definition_version_id: UUID,
    steps: object,
    expected_revision: int,
    idempotency_key: UUID,
) -> WorkflowDefinitionVersion:
    """Editing invalidates validation; published content has no edit path."""
    checked = validate_steps(steps)
    definition = _definition(
        clinic_id=clinic_id, definition_version_id=definition_version_id
    )
    if not _command(
        definition=definition,
        action="revise",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        terms=checked,
    ):
        return definition
    if definition.state not in (
        WorkflowDefinitionVersion.State.DRAFT,
        WorkflowDefinitionVersion.State.VALIDATED,
    ):
        raise WorkflowConflictError
    definition.steps = checked
    definition.fingerprint = digest(
        {
            "actor": str(definition.created_by_id),
            "key": definition.key,
            "steps": checked,
        }
    )
    definition.state = WorkflowDefinitionVersion.State.DRAFT
    definition.validated_at = None
    _save(definition, "workflows.definition.revised")
    return definition


@transaction.atomic
def validate_definition(
    *,
    clinic_id: UUID,
    definition_version_id: UUID,
    expected_revision: int,
    idempotency_key: UUID,
) -> WorkflowDefinitionVersion:
    """Record successful closed-schema validation on this exact revision."""
    definition = _definition(
        clinic_id=clinic_id, definition_version_id=definition_version_id
    )
    checked = validate_steps(definition.steps)
    if not _command(
        definition=definition,
        action="validate",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        terms=definition.fingerprint,
    ):
        return definition
    if definition.state != WorkflowDefinitionVersion.State.DRAFT:
        raise WorkflowConflictError
    if checked != definition.steps:
        raise WorkflowConflictError
    definition.state = WorkflowDefinitionVersion.State.VALIDATED
    definition.validated_at = timezone.now()
    _save(definition, "workflows.definition.validated")
    return definition


@transaction.atomic
def publish_definition_draft(
    *,
    clinic_id: UUID,
    definition_version_id: UUID,
    expected_revision: int,
    idempotency_key: UUID,
) -> WorkflowDefinitionVersion:
    """Publish only the exact validated revision; content becomes immutable."""
    definition = _definition(
        clinic_id=clinic_id, definition_version_id=definition_version_id
    )
    if not _command(
        definition=definition,
        action="publish",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        terms=definition.fingerprint,
    ):
        return definition
    if definition.state != WorkflowDefinitionVersion.State.VALIDATED:
        raise WorkflowConflictError
    definition.state = WorkflowDefinitionVersion.State.PUBLISHED
    definition.published_by_id = require_manager(clinic_id=clinic_id)
    definition.published_at = timezone.now()
    _save(definition, "workflows.definition.published")
    return definition


@transaction.atomic
def retire_definition(
    *,
    clinic_id: UUID,
    definition_version_id: UUID,
    expected_revision: int,
    idempotency_key: UUID,
) -> WorkflowDefinitionVersion:
    """Prevent new runs without changing the pins or effects of existing runs."""
    definition = _definition(
        clinic_id=clinic_id, definition_version_id=definition_version_id
    )
    if not _command(
        definition=definition,
        action="retire",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        terms=definition.fingerprint,
    ):
        return definition
    if definition.state != WorkflowDefinitionVersion.State.PUBLISHED:
        raise WorkflowConflictError
    definition.state = WorkflowDefinitionVersion.State.RETIRED
    definition.retired_at = timezone.now()
    _save(definition, "workflows.definition.retired")
    return definition
