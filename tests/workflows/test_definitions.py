"""Definition commands preserve creation terms and immutable published pins."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import WorkflowDefinitionVersion, WorkflowRun, WorkflowStep
from apps.workflows.services import (
    WorkflowConflictError,
    create_definition,
    publish_definition,
    publish_definition_draft,
    retire_definition,
    revise_definition,
    start_run,
    validate_definition,
)
from django.db import DatabaseError, transaction

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def workflow_manager(rbac_graph: RbacGraph) -> RbacGraph:
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.CLINIC_MANAGER,
            workflow_bundle_version=2,
        )
    return rbac_graph


def test_validation_is_revision_bound_and_edit_invalidates_it(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        draft = create_definition(
            clinic_id=graph.clinic_a,
            key="draft",
            steps=[{"handler": "timer", "seconds": 1}],
            idempotency_key=uuid4(),
        )
        assert draft.state == WorkflowDefinitionVersion.State.DRAFT
        with pytest.raises(WorkflowConflictError):
            publish_definition_draft(
                clinic_id=graph.clinic_a,
                definition_version_id=draft.pk,
                expected_revision=draft.revision,
                idempotency_key=uuid4(),
            )
        validated = validate_definition(
            clinic_id=graph.clinic_a,
            definition_version_id=draft.pk,
            expected_revision=draft.revision,
            idempotency_key=uuid4(),
        )
        assert validated.validated_at is not None
        edited = revise_definition(
            clinic_id=graph.clinic_a,
            definition_version_id=draft.pk,
            steps=[{"handler": "timer", "seconds": 2}],
            expected_revision=validated.revision,
            idempotency_key=uuid4(),
        )
        assert edited.state == WorkflowDefinitionVersion.State.DRAFT
        assert edited.validated_at is None
        with pytest.raises(WorkflowConflictError):
            validate_definition(
                clinic_id=graph.clinic_a,
                definition_version_id=draft.pk,
                expected_revision=validated.revision,
                idempotency_key=uuid4(),
            )
        assert WorkflowRun.objects.count() == WorkflowStep.objects.count() == 0


def test_creation_replay_preserves_original_terms_after_draft_edit(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    key = uuid4()
    original = [{"handler": "timer", "seconds": 1}]
    changed = [{"handler": "timer", "seconds": 2}]
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        draft = create_definition(
            clinic_id=graph.clinic_a,
            key="editable",
            steps=original,
            idempotency_key=key,
        )
        edited = revise_definition(
            clinic_id=graph.clinic_a,
            definition_version_id=draft.pk,
            steps=changed,
            expected_revision=draft.revision,
            idempotency_key=uuid4(),
        )
        replay = create_definition(
            clinic_id=graph.clinic_a,
            key="editable",
            steps=original,
            idempotency_key=key,
        )
        assert replay.pk == edited.pk
        assert replay.steps == changed
        assert replay.revision == edited.revision
        assert replay.creation_fingerprint != replay.fingerprint
        with pytest.raises(WorkflowConflictError):
            create_definition(
                clinic_id=graph.clinic_a,
                key="editable",
                steps=changed,
                idempotency_key=key,
            )
        assert WorkflowDefinitionVersion.objects.filter(key="editable").count() == 1


def test_edited_draft_cannot_replay_convenience_publication(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    key = uuid4()
    original = [{"handler": "timer", "seconds": 1}]
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        draft = create_definition(
            clinic_id=graph.clinic_a, key="edited", steps=original, idempotency_key=key
        )
        edited = revise_definition(
            clinic_id=graph.clinic_a,
            definition_version_id=draft.pk,
            steps=[{"handler": "timer", "seconds": 2}],
            expected_revision=draft.revision,
            idempotency_key=uuid4(),
        )
        with pytest.raises(WorkflowConflictError):
            publish_definition(
                clinic_id=graph.clinic_a,
                key="edited",
                steps=original,
                idempotency_key=key,
            )
        edited.refresh_from_db()
        assert edited.state == WorkflowDefinitionVersion.State.DRAFT
        assert edited.revision == 2


def test_retirement_prevents_new_runs_but_preserves_exact_existing_replay(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    run_key = uuid4()
    refs = [{"kind": "clinic", "id": str(graph.clinic_a)}]
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        first = publish_definition(
            clinic_id=graph.clinic_a,
            key="pin",
            steps=[{"handler": "timer", "seconds": 1}],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=first.pk,
            context_refs=refs,
            idempotency_key=run_key,
        )
        retire_definition(
            clinic_id=graph.clinic_a,
            definition_version_id=first.pk,
            expected_revision=first.revision,
            idempotency_key=uuid4(),
        )
        replay = start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=first.pk,
            context_refs=refs,
            idempotency_key=run_key,
        )
        assert replay.pk == run.pk
        with pytest.raises(WorkflowConflictError):
            start_run(
                clinic_id=graph.clinic_a,
                definition_version_id=first.pk,
                context_refs=refs,
                idempotency_key=uuid4(),
            )
        successor = publish_definition(
            clinic_id=graph.clinic_a,
            key="pin",
            steps=[{"handler": "timer", "seconds": 2}],
            idempotency_key=uuid4(),
        )
        run.refresh_from_db()
        assert successor.version == first.version + 1
        assert run.definition_version_id == first.pk
        assert WorkflowRun.objects.count() == 1


def test_published_content_has_service_and_database_edit_guards(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        definition = publish_definition(
            clinic_id=graph.clinic_a,
            key="immutable",
            steps=[{"handler": "timer", "seconds": 1}],
            idempotency_key=uuid4(),
        )
        with pytest.raises(WorkflowConflictError):
            revise_definition(
                clinic_id=graph.clinic_a,
                definition_version_id=definition.pk,
                steps=[{"handler": "timer", "seconds": 2}],
                expected_revision=definition.revision,
                idempotency_key=uuid4(),
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            WorkflowDefinitionVersion.objects.filter(pk=definition.pk).update(
                steps=[{"handler": "timer", "seconds": 2}],
                revision=definition.revision + 1,
                last_command_key=uuid4(),
                last_command_digest="a" * 64,
            )
        definition.refresh_from_db()
        assert definition.steps == [{"handler": "timer", "seconds": 1}]
        assert definition.state == WorkflowDefinitionVersion.State.PUBLISHED
