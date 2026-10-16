"""Immutable publication and explicitly pinned workflow run commands."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid5

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.core.integration import hold_subject_mutation_lock
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_clinic_access,
    require_manager,
    require_reference,
)
from apps.workflows.definition_services import (
    create_definition,
    publish_definition_draft,
    validate_definition,
)
from apps.workflows.models import WorkflowDefinitionVersion, WorkflowRun, WorkflowStep
from apps.workflows.validation import (
    MAX_STEPS,
    WorkflowConflictError,
    WorkflowInputError,
    digest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID


@transaction.atomic
def publish_definition(
    *, clinic_id: UUID, key: str, steps: object, idempotency_key: UUID
) -> WorkflowDefinitionVersion:
    """Create, validate and publish one exact version with replay-stable commands."""
    definition = create_definition(
        clinic_id=clinic_id, key=key, steps=steps, idempotency_key=idempotency_key
    )
    if definition.fingerprint != definition.creation_fingerprint:
        raise WorkflowConflictError
    if definition.state == WorkflowDefinitionVersion.State.DRAFT:
        definition = validate_definition(
            clinic_id=clinic_id,
            definition_version_id=definition.pk,
            expected_revision=definition.revision,
            idempotency_key=uuid5(idempotency_key, "validate"),
        )
    if definition.state == WorkflowDefinitionVersion.State.VALIDATED:
        definition = publish_definition_draft(
            clinic_id=clinic_id,
            definition_version_id=definition.pk,
            expected_revision=definition.revision,
            idempotency_key=uuid5(idempotency_key, "publish"),
        )
    return definition


@transaction.atomic
def start_run(
    *,
    clinic_id: UUID,
    definition_version_id: UUID,
    context_refs: Sequence[Mapping[str, object]],
    idempotency_key: UUID,
) -> WorkflowRun:
    """Pin exactly the requested immutable version, never the latest at execution."""
    require_manager(clinic_id=clinic_id)
    clinic, actor = require_clinic_access(
        clinic_id=clinic_id, permission="tasks.assign"
    )
    definition = (
        WorkflowDefinitionVersion.objects.select_for_update()
        .filter(pk=definition_version_id, clinic_id=clinic_id)
        .first()
    )
    if definition is None:
        raise WorkflowAccessDeniedError
    if not 1 <= len(context_refs) <= MAX_STEPS:
        raise WorkflowInputError
    refs = [require_reference(clinic_id=clinic_id, value=ref) for ref in context_refs]
    for step in definition.steps:
        if step["handler"] == "task" and step["subject"] >= len(refs):
            raise WorkflowInputError
    fingerprint = digest(
        {"definition": str(definition.pk), "actor": str(actor), "refs": refs}
    )
    previous = WorkflowRun.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if previous is not None:
        if previous.fingerprint != fingerprint:
            raise WorkflowConflictError
        return previous
    if definition.state != WorkflowDefinitionVersion.State.PUBLISHED:
        raise WorkflowConflictError
    run, created = WorkflowRun.objects.get_or_create(
        clinic_id=clinic_id,
        idempotency_key=idempotency_key,
        defaults={
            "organization_id": clinic.organization_id,
            "definition_version": definition,
            "started_by_id": actor,
            "context_refs": refs,
            "fingerprint": fingerprint,
        },
    )
    if run.fingerprint != fingerprint:
        raise WorkflowConflictError
    if created:
        WorkflowStep.objects.bulk_create(
            [
                WorkflowStep(
                    organization_id=clinic.organization_id,
                    clinic=clinic,
                    run=run,
                    position=position,
                )
                for position in range(len(definition.steps))
            ]
        )
        record_phase1_event(
            "workflows.run.started", clinic_id=clinic_id, affected_record_id=run.pk
        )
    # Beat is the durable publisher. No broker outage can turn a committed run
    # into an HTTP error, and no long-lived ETA or uncommitted row is published.
    return run


@transaction.atomic
def cancel_run(*, clinic_id: UUID, run_id: UUID) -> WorkflowRun:
    """Cancel only before further work; serialize with every step claim/effect."""
    require_manager(clinic_id=clinic_id)
    run = (
        WorkflowRun.objects.select_for_update()
        .filter(pk=run_id, clinic_id=clinic_id)
        .first()
    )
    if run is None:
        raise WorkflowAccessDeniedError
    if run.state == "cancelled":
        return run
    if run.state in {"completed", "failed"}:
        raise WorkflowConflictError
    # Lock the same rows the executor needs. An already committed external
    # outbox intent has its own subject recheck before send.
    steps = tuple(
        WorkflowStep.objects.select_for_update().filter(run=run).order_by("position")
    )
    for step in steps:
        if step.state not in {"completed", "failed", "cancelled"}:
            hold_subject_mutation_lock("workflows.step", step.pk)
            step.state = "cancelled"
            step.save(update_fields=("state", "updated_at"))
            record_phase1_event(
                "workflows.step.cancelled",
                clinic_id=clinic_id,
                affected_record_id=step.pk,
            )
    run.state = "cancelled"
    run.revision += 1
    run.save(update_fields=("state", "revision", "updated_at"))
    record_phase1_event(
        "workflows.run.cancelled", clinic_id=clinic_id, affected_record_id=run.pk
    )
    return run
