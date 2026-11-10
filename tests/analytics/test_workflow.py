"""The fenced workflow step must own a real, exact metric refresh receipt."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.analytics.models import ReadModelRefresh
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.engine import apply_claim, claim_step
from apps.workflows.models import WorkflowStep
from apps.workflows.services import create_definition, publish_definition, start_run
from apps.workflows.validation import WorkflowInputError, validate_steps
from django.db import DatabaseError, transaction

from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_refresh_step_requires_its_exact_effect_and_replays_once(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        definition = publish_definition(
            clinic_id=graph.clinic_a,
            key="metrics",
            steps=[{"handler": "metrics_refresh", "metric_id": "no_show", "days": 30}],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[{"kind": "clinic", "id": str(graph.clinic_a)}],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
        fence = claim_step(step_id=step.pk)
        assert type(fence) is int
        with pytest.raises(DatabaseError), transaction.atomic():
            WorkflowStep.objects.filter(pk=step.pk).update(
                state="completed", claim_until=None
            )
        assert not ReadModelRefresh.objects.exists()
        assert apply_claim(step_id=step.pk, fencing_token=fence) == "completed"
        assert apply_claim(step_id=step.pk, fencing_token=fence) == "stale"
        refresh = ReadModelRefresh.objects.get(command_id=step.pk)
        assert refresh.window_end == run.created_at
        assert refresh.descriptor["window"]["end"] == run.created_at.astimezone(
            UTC
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        run.refresh_from_db()
        assert run.state == "completed"


@pytest.mark.parametrize(
    "step",
    [
        {"handler": "metrics_refresh", "metric_id": "all_clinics", "days": 30},
        {"handler": "metrics_refresh", "metric_id": "no_show", "days": True},
        {"handler": "metrics_refresh", "metric_id": "no_show", "days": 367},
        {"handler": "metrics_refresh", "metric_id": "no_show", "days": 0},
        {
            "handler": "metrics_refresh",
            "metric_id": "no_show",
            "days": 30,
            "sql": "SELECT 1",
        },
    ],
)
def test_refresh_step_schema_rejects_unbounded_or_model_written_work(
    step: object,
) -> None:
    with pytest.raises(WorkflowInputError):
        validate_steps([step])


def test_native_designer_authors_a_typed_metric_step(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        definition = create_definition(
            clinic_id=graph.clinic_a,
            key="metric-refresh",
            steps=[{"handler": "timer", "seconds": 0}],
            idempotency_key=uuid4(),
        )
    client = verified_client(graph)
    url = f"/clinics/{graph.clinic_a}/workflows/designer/"
    with runtime_role():
        page = client.get(url)
        assert page.status_code == 200
        assert b"metrics_refresh" in page.content
        prefix = str(definition.pk)
        response = client.post(
            url,
            {
                "action": "add-step",
                "definition_id": prefix,
                "expected_revision": definition.revision,
                "idempotency_key": str(uuid4()),
                f"{prefix}-handler": "metrics_refresh",
                f"{prefix}-task_kind": "checklist",
                f"{prefix}-owner_role": "receptionist",
                f"{prefix}-subject": 0,
                f"{prefix}-seconds": 0,
                f"{prefix}-offset": 0,
                f"{prefix}-metric_id": "no_show",
                f"{prefix}-metric_days": 30,
            },
        )
        assert response.status_code == 302
        rendered = client.get(url)
    (tmp_path / "designer.html").write_bytes(rendered.content)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        definition.refresh_from_db()
        assert definition.steps[-1] == {
            "handler": "metrics_refresh",
            "metric_id": "no_show",
            "days": 30,
        }
