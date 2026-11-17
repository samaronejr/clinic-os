"""Staged approval is clinic-bound; shadow observations never enter domain flows."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.adapters import SyntheticProvider, register_provider
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.evaluation import services
from apps.ai.evaluation.runner import evaluate
from apps.ai.evaluation.versions import source_versions
from apps.ai.models import AIInvocation, EvaluationRun, ModelChangeRequest
from apps.ai.owner import set_global_switch, transition_model
from apps.ai.policy import check_configuration
from apps.core.integration import execute_operation
from apps.identity.models import UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from django.apps import apps
from django.db import DatabaseError, transaction
from django.utils import timezone
from psycopg import sql

from ai.evaluation_support import (
    as_reviewer,
    candidate,
    complete_reviews,
    evaluation_world,
    review_world,
)
from ai.support import enqueue, model, seed, set_policy
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest, ProviderResult

    from ai.evaluation_support import EvaluationWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def approve_stage(world: EvaluationWorld) -> ModelChangeRequest:
    assert complete_reviews(world).status == "passed"
    with as_reviewer(world, world.owner):
        change = services.request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver):
        return services.decide_change(
            clinic_id=world.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-accountable-stage-review",
        )


def stage_world(world: EvaluationWorld, change: ModelChangeRequest) -> EvaluationWorld:
    with as_reviewer(world, world.owner):
        run = services.start_stage_run(
            clinic_id=world.clinic,
            change_id=change.pk,
            reviewers=world.reviewers,
        )
    results = evaluate(
        capability=world.protocol.capability,
        corpus_version=world.protocol.corpus_version,
        provider_key=world.configuration.provider,
        model_id=world.configuration.model_id,
        purpose=Purpose(world.configuration.purpose),
    )
    with as_reviewer(world, world.owner):
        run = services.store_results(
            clinic_id=world.clinic, run_id=run.pk, results=results
        )
    return replace(world, run=run)


def test_offline_approval_cannot_skip_shadow(rbac_graph: RbacGraph) -> None:
    world = evaluation_world(rbac_graph)
    approve_stage(world)
    with pytest.raises(DatabaseError), transaction.atomic():
        transition_model(configuration_id=world.configuration.pk, state="active")


def test_progression_requires_reviewed_preceding_stage_and_separate_autonomy(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    shadow = approve_stage(world)
    assert shadow.stage == "shadow"
    assert shadow.exposure == "none"
    observed = stage_world(world, shadow)
    assert observed.run.stage == "shadow"
    with as_reviewer(world, world.owner):
        pending = services.request_change(
            clinic_id=world.clinic, run_id=observed.run.pk
        )
    with (
        as_reviewer(world, world.approver),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        services.decide_change(
            clinic_id=world.clinic,
            change_id=pending.pk,
            decision="approved",
            approval_ref="synthetic-unreviewed-promotion",
        )
    complete_reviews(observed)
    with as_reviewer(world, world.approver):
        live = services.decide_change(
            clinic_id=world.clinic,
            change_id=pending.pk,
            decision="approved",
            approval_ref="synthetic-reviewed-shadow-receipt",
        )
    assert live.stage == "reviewed_live"
    assert live.exposure == "human_review"
    configuration = transition_model(
        configuration_id=world.configuration.pk, state="active"
    )
    with as_reviewer(world, world.owner):
        assert services.rollout_status(clinic_id=world.clinic, change_id=live.pk) == (
            "reviewed_live"
        )
        assert not services.autonomy_allowed(
            clinic_id=world.clinic, configuration_id=configuration.pk
        )
    monitored = approve_stage(stage_world(world, live))
    assert monitored.stage == "monitored_autonomy"
    assert monitored.exposure == "bounded_admin"
    with as_reviewer(world, world.owner):
        assert services.autonomy_allowed(
            clinic_id=world.clinic, configuration_id=configuration.pk
        )


@pytest.mark.parametrize("foreign", [True, False])
def test_stage_receipt_cannot_be_foreign_or_stale(
    rbac_graph: RbacGraph, foreign: bool
) -> None:
    world = evaluation_world(rbac_graph)
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    shadow = approve_stage(world)
    if foreign:
        with (
            as_reviewer(world, world.owner),
            pytest.raises(GatewayAccessDeniedError),
        ):
            services.start_stage_run(
                clinic_id=rbac_graph.clinic_c,
                change_id=shadow.pk,
                reviewers=world.reviewers,
            )
    else:
        with as_reviewer(world, world.owner):
            services.stop_rollout(
                clinic_id=world.clinic, change_id=shadow.pk, state="invalidated"
            )
        with (
            as_reviewer(world, world.owner),
            pytest.raises(GatewayRefusedError),
        ):
            services.start_stage_run(
                clinic_id=world.clinic,
                change_id=shadow.pk,
                reviewers=world.reviewers,
            )


def test_kill_is_durable_across_reenable_and_requires_new_stage_evidence(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    shadow = approve_stage(world)
    set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    with as_reviewer(world, world.owner):
        assert (
            services.rollout_status(clinic_id=world.clinic, change_id=shadow.pk)
            == "killed"
        )
        with pytest.raises(GatewayRefusedError):
            services.start_stage_run(
                clinic_id=world.clinic,
                change_id=shadow.pk,
                reviewers=world.reviewers,
            )


def test_native_skipped_stage_and_foreign_receipt_inserts_are_refused(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    complete_reviews(world)
    with as_reviewer(world, world.owner):
        for clinic in (world.clinic, rbac_graph.clinic_b):
            with pytest.raises(DatabaseError), transaction.atomic():
                ModelChangeRequest.objects.create(
                    organization_id=world.organization,
                    clinic_id=clinic,
                    configuration=world.configuration,
                    run=world.run,
                    requested_by_id=world.owner,
                    stage="reviewed_live",
                    exposure="human_review",
                )


def test_revoked_approval_membership_invalidates_the_stage_and_its_receipt(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    shadow = approve_stage(world)
    observed = stage_world(world, shadow)
    with owner_context(world.organization):
        UserClinicRole.objects.filter(
            clinic_id=world.clinic, user_id=world.approver, role="owner"
        ).update(revoked_at=timezone.now())
    with as_reviewer(world, world.owner):
        assert (
            services.rollout_status(clinic_id=world.clinic, change_id=shadow.pk)
            == "invalidated"
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            services.request_change(clinic_id=world.clinic, run_id=observed.run.pk)
    with owner_context(world.organization):
        UserClinicRole.objects.create(
            organization_id=world.organization,
            clinic_id=world.clinic,
            user_id=world.approver,
            role="owner",
        )
    with as_reviewer(world, world.owner):
        assert (
            services.rollout_status(clinic_id=world.clinic, change_id=shadow.pk)
            == "invalidated"
        )


@pytest.mark.parametrize("global_stop", [True, False])
def test_old_queued_permission_never_revives_after_reenable(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, global_stop: bool
) -> None:
    world = seed(rbac_graph, monkeypatch)
    admitted = enqueue(world)
    assert admitted.invocation_id is not None
    assert admitted.operation_id is not None
    if global_stop:
        set_global_switch(enabled=False, approved_regions=("br-synthetic",))
        set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    else:
        set_policy(world, cap=10_000_000, enabled=False)
        set_policy(world, cap=10_000_000, enabled=True)
    assert enqueue(world).reason == "configuration_unavailable"
    with runtime_role():
        assert execute_operation(admitted.operation_id) == "cancelled"
    with owner_context(world.graph.organization_a):
        invocation = AIInvocation.objects.get(pk=admitted.invocation_id)
        assert invocation.status == "failed"
        assert not hasattr(invocation, "output")


def test_clinical_review_permission_cannot_expand_to_administrative_autonomy(
    rbac_graph: RbacGraph,
) -> None:
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=candidate(purpose=Purpose.CARE_COORDINATION),
    )
    shadow = approve_stage(world)
    live = approve_stage(stage_world(world, shadow))
    monitored = stage_world(world, live)
    complete_reviews(monitored)
    with (
        as_reviewer(world, world.owner),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        services.request_change(clinic_id=world.clinic, run_id=monitored.run.pk)


def test_shadow_configuration_cannot_enter_normal_inference_or_domain_projection(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed(rbac_graph, monkeypatch)
    shadow_world = evaluation_world(rbac_graph)
    approve_stage(shadow_world)
    result = enqueue(world)
    assert result.reason == "configuration_unavailable"
    assert result.invocation_id is None
    assert result.operation_id is None
    assert world.publications == []


def domain_counts(database_url: str) -> dict[str, tuple[int, str]]:
    excluded = {
        "audit_event",
        "ai_evaluationprotocol",
        "ai_evaluationrun",
        "ai_evaluationrating",
        "ai_modelchangerequest",
    }
    tables = sorted(
        {
            row._meta.db_table
            for row in apps.get_models()
            if row._meta.managed and row._meta.db_table not in excluded
        }
    )
    counts: dict[str, tuple[int, str]] = {}
    with psycopg.connect(database_url) as inspection, inspection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                sql.SQL(
                    "SELECT count(*),encode(clinic_app.digest(convert_to("
                    "coalesce(string_agg(to_jsonb(t)::text,E'\\n' "
                    "ORDER BY to_jsonb(t)::text),''),'UTF8'),'sha256'),'hex') "
                    "FROM {} t"
                ).format(sql.Identifier("clinic_app", table))
            )
            row = cursor.fetchone()
            assert row is not None
            counts[table] = (row[0], row[1])
    return counts


@pytest.mark.parametrize(
    "effect",
    [
        "ehr.finalize",
        "ehr.write",
        "prescription.issue",
        "orders.file",
        "scheduling.book",
        "comms.send",
        "workflows.create",
        "billing.refund",
        "insurance.upcode",
        "metrics.sql",
    ],
)
def test_each_forbidden_shadow_effect_has_zero_domain_side_effects(
    rbac_graph: RbacGraph, effect: str, superuser_database_url: str
) -> None:
    world = evaluation_world(rbac_graph)
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    shadow = approve_stage(world)

    class EffectAttempt(SyntheticProvider):
        def complete(self, *, request: ProviderRequest) -> ProviderResult:
            result = super().complete(request=request)
            return replace(
                result, content=json.dumps({"tool": effect, "argument": str(uuid4())})
            )

    register_provider(EffectAttempt())
    before = domain_counts(superuser_database_url)
    observed = stage_world(world, shadow)
    assert observed.run.sample_results["tool_calls"] == 0
    assert domain_counts(superuser_database_url) == before
    with owner_context(world.organization):
        assert EvaluationRun.objects.get(pk=observed.run.pk).stage == "shadow"
    with pytest.raises(DatabaseError), transaction.atomic():
        transition_model(configuration_id=world.configuration.pk, state="active")


@pytest.mark.parametrize(
    ("purpose", "dependency"),
    [
        (Purpose.CARE_COORDINATION, "apps/ai/care_contracts.py"),
        (Purpose.CARE_COORDINATION, "apps/ai/care_coordination.py"),
        (Purpose.ANALYST, "apps/analytics/analyst_contracts.py"),
        (Purpose.ANALYST, "apps/analytics/contracts.py"),
    ],
)
def test_approved_admission_refuses_each_previously_omitted_dependency(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    purpose: Purpose,
    dependency: str,
) -> None:
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-rollout-drift-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    configuration = model(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        purpose=purpose,
    )
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=configuration,
    )
    with as_reviewer(world, world.owner):
        check_configuration(configuration, world.clinic)
    original = Path.read_bytes

    def changed(path: Path) -> bytes:
        content = original(path)
        if str(path).endswith(dependency):
            return content + b"\n# synthetic drift\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert source_versions(purpose)
    with as_reviewer(world, world.owner), pytest.raises(GatewayRefusedError):
        check_configuration(configuration, world.clinic)
