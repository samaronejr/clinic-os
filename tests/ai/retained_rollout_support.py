"""Invalidate a real succeeded invocation without changing its domain sources."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from apps.ai.controls import set_organization_policy
from apps.ai.evaluation.rollout import stop_rollout
from apps.ai.evaluation.services import ReviewerAssignment
from apps.ai.evaluation.versions import source_versions
from apps.ai.models import AIInvocation
from apps.ai.owner import set_global_switch
from apps.ai.policy import check_configuration, parse_policy
from apps.core.secrets import secret_store
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import connection
from django.utils import timezone

from ai.evaluation_support import EvaluationWorld, as_reviewer, promote_stage
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    import pytest
    from apps.ai.contracts import Purpose

    from rbac_fixtures import RbacGraph

Invalidation = Literal[
    "source_drift",
    "global_kill",
    "organization_kill",
    "revoked_stage",
    "advanced_stage",
    "foreign_clinic",
]
INVALIDATIONS: tuple[Invalidation, ...] = (
    "source_drift",
    "global_kill",
    "organization_kill",
    "revoked_stage",
    "advanced_stage",
    "foreign_clinic",
)


def authorize_native(*, invocation_id: UUID, clinic_id: UUID, purpose: Purpose) -> bool:
    """Supply fresh encrypted source facts directly; bypass every Python AI gate."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_current_xact_id()::text")
        transaction_id = cursor.fetchone()[0]
    receipt = protect(
        purpose="ai.effect.sources",
        plaintext=json.dumps(
            {
                "subject": str(invocation_id),
                "transaction": transaction_id,
                "sources": source_versions(purpose),
            },
            separators=(",", ":"),
        ).encode(),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_authorize_effect(%s,%s,%s,%s)",
            [
                invocation_id,
                clinic_id,
                receipt,
                secret_store().get_secret(KEK_SECRET_NAME),
            ],
        )
        return bool(cursor.fetchone() == (True,))


def invalidate(
    graph: RbacGraph,
    invocation_id: UUID,
    mode: Invalidation,
    monkeypatch: pytest.MonkeyPatch,
) -> UUID:
    """Keep output intact and return the clinic scope used to attempt acceptance."""
    with owner_context(graph.organization_a):
        invocation = AIInvocation.objects.select_related(
            "configuration", "rollout_approval__run__protocol"
        ).get(pk=invocation_id)
        approval = invocation.rollout_approval
        assert invocation.status == "succeeded"
        assert approval is not None
        assert approval.approved_by_id is not None
    if mode == "source_drift":
        read_bytes = Path.read_bytes

        def changed(path: Path) -> bytes:
            content = read_bytes(path)
            if path.as_posix().endswith("apps/ai/policy.py"):
                return content + b"\n# retained-output source drift\n"
            return content

        monkeypatch.setattr(Path, "read_bytes", changed)
    elif mode == "global_kill":
        set_global_switch(enabled=False, approved_regions=("br-synthetic",))
        set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    elif mode == "organization_kill":
        with owner_context(graph.organization_a):
            current = latest_configuration(graph.clinic_a)
            assert current is not None
        with (
            runtime_role(),
            tenant_context(current.published_by_id, graph.organization_a),
        ):
            disabled = set_organization_policy(
                clinic_id=graph.clinic_a,
                expected_version=current.version,
                policy=replace(parse_policy(current.ai_policy), enabled=False),
            )
            set_organization_policy(
                clinic_id=graph.clinic_a,
                expected_version=disabled.version,
                policy=parse_policy(current.ai_policy),
            )
    elif mode == "revoked_stage":
        with owner_context(graph.organization_a):
            assert UserClinicRole.objects.filter(
                clinic_id=graph.clinic_a,
                user_id=approval.approved_by_id,
                role="owner",
                revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
    elif mode == "foreign_clinic":
        return graph.clinic_b
    else:
        assert mode == "advanced_stage"
        run = approval.run
        assert run.rollout_approval_id is not None
        world = EvaluationWorld(
            graph.organization_a,
            graph.clinic_a,
            approval.requested_by_id,
            approval.approved_by_id,
            ReviewerAssignment(run.rater_one_id, run.rater_two_id, run.adjudicator_id),
            invocation.configuration,
            run.protocol,
            run,
        )
        with as_reviewer(world, world.owner):
            stop_rollout(
                clinic_id=world.clinic, change_id=approval.pk, state="invalidated"
            )
        successor = promote_stage(world, run.rollout_approval_id)
        assert successor.pk != approval.pk
        with as_reviewer(world, world.owner):
            # A new valid permission for this same active model must not rescue
            # output that was admitted under the now-invalidated permission.
            check_configuration(invocation.configuration, world.clinic)
    return graph.clinic_a
