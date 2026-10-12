"""Native POST commands commit the same real lifecycle as domain services."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task, WorkflowDefinitionVersion, WorkflowRun
from django.urls import reverse

from identity.permission_support import owner_context
from renewal.test_retention import staff_client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def workflow_manager(rbac_graph: RbacGraph) -> RbacGraph:
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_MANAGER,
            defaults={"workflow_bundle_version": 2},
        )
    return rbac_graph


def test_task_native_create_assign_start_complete_commits_evidence(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    key = uuid4()
    url = reverse("workflows:tasks", args=(graph.clinic_a,))
    with staff_client(graph.clinic_admin) as client:
        assert client.session["active_org_id"] == str(graph.organization_a)
        assert (
            client.post(
                url,
                {
                    "action": "create",
                    "kind": "checklist",
                    "priority": "normal",
                    "due_date": "02/06/2035",
                    "due_time": "09:00",
                    "subject_kind": "clinic",
                    "subject_id": str(graph.clinic_a),
                    "idempotency_key": str(key),
                },
            ).status_code
            == 200
        )
        with tenant_context(graph.clinic_admin, graph.organization_a):
            task = Task.objects.get(clinic_id=graph.clinic_a, idempotency_key=key)
        for action, revision, extra in (
            ("assign", 1, {"owner": "me"}),
            ("start", 2, {}),
            ("complete", 3, {"checked": "on"}),
        ):
            assert (
                client.post(
                    url,
                    {
                        "action": action,
                        "task_id": str(task.pk),
                        "expected_revision": str(revision),
                        **extra,
                    },
                ).status_code
                == 200
            )
        with tenant_context(graph.clinic_admin, graph.organization_a):
            task.refresh_from_db()
            assert task.state == "done"
            assert task.owner_user_id == graph.clinic_admin
            assert task.completion_evidence == {"checked": True}
            assert task.revision == 4
        comment_key = uuid4()
        for _ in range(2):
            assert (
                client.post(
                    url,
                    {
                        "action": "comment",
                        "task_id": str(task.pk),
                        "expected_revision": "4",
                        "idempotency_key": str(comment_key),
                        "body": "Synthetic operational check",
                    },
                ).status_code
                == 200
            )
        with tenant_context(graph.clinic_admin, graph.organization_a):
            assert task.comments.count() == 1
            assert (
                task.comments.get(pk=comment_key).body == "Synthetic operational check"
            )


def test_designer_native_versions_and_run_pin_survive_retirement(
    workflow_manager: RbacGraph,
) -> None:
    graph = workflow_manager
    key = uuid4()
    designer = reverse("workflows:designer", args=(graph.clinic_a,))
    runs = reverse("workflows:runs", args=(graph.clinic_a,))
    with staff_client(graph.clinic_admin) as client:
        assert client.session["active_org_id"] == str(graph.organization_a)
        assert client.get(designer).status_code == 200
        assert (
            client.post(
                designer,
                {
                    "action": "create",
                    "key": "native-discharge",
                    "template": "appointment-discharge",
                    "idempotency_key": str(key),
                },
            ).status_code
            == 302
        )
        with tenant_context(graph.clinic_admin, graph.organization_a):
            definition = WorkflowDefinitionVersion.objects.get(
                clinic_id=graph.clinic_a, idempotency_key=key
            )
            assert [step["handler"] for step in definition.steps] == [
                "task",
                "timer",
                "task",
            ]
        for action, revision in (("validate", 1), ("publish", 2)):
            assert (
                client.post(
                    designer,
                    {
                        "action": action,
                        "definition_id": str(definition.pk),
                        "expected_revision": str(revision),
                        "idempotency_key": str(uuid4()),
                    },
                ).status_code
                == 302
            )
        assert client.get(runs).status_code == 200
        run_key = uuid4()
        assert (
            client.post(
                runs,
                {
                    "action": "start",
                    "definition_version_id": str(definition.pk),
                    "subject_kind": "clinic",
                    "subject_id": str(graph.clinic_a),
                    "idempotency_key": str(run_key),
                },
            ).status_code
            == 302
        )
        assert (
            client.post(
                designer,
                {
                    "action": "retire",
                    "definition_id": str(definition.pk),
                    "expected_revision": "3",
                    "idempotency_key": str(uuid4()),
                },
            ).status_code
            == 302
        )
        with tenant_context(graph.clinic_admin, graph.organization_a):
            definition.refresh_from_db()
            run = WorkflowRun.objects.get(
                clinic_id=graph.clinic_a, idempotency_key=run_key
            )
            assert definition.state == "retired"
            assert run.definition_version_id == definition.pk
            assert run.steps.count() == 3
        assert (
            client.post(
                runs,
                {
                    "action": "start",
                    "definition_version_id": str(definition.pk),
                    "subject_kind": "clinic",
                    "subject_id": str(graph.clinic_a),
                    "idempotency_key": str(uuid4()),
                },
            ).status_code
            == 409
        )
