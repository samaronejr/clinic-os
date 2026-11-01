"""Real-role engineering review fixtures, never a product activation bypass."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.ai.contracts import Price, Purpose
from apps.ai.evaluation.corpus import load_corpus
from apps.ai.evaluation.protocol import ENGINEERING_THRESHOLDS, ERROR_LABELS
from apps.ai.evaluation.runner import evaluate
from apps.ai.evaluation.services import (
    PURPOSE_CAPABILITIES,
    ReviewerAssignment,
    decide_change,
    finish_run,
    rate_case,
    register_protocol,
    request_change,
    start_run,
    store_results,
)
from apps.ai.models import EvaluationProtocol, ModelConfiguration
from apps.ai.owner import ModelTerms, propose_model, transition_model
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.ai.models import EvaluationRun

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class EvaluationWorld:
    organization: UUID
    clinic: UUID
    owner: UUID
    approver: UUID
    reviewers: ReviewerAssignment
    configuration: ModelConfiguration
    protocol: EvaluationProtocol
    run: EvaluationRun


@contextmanager
def as_reviewer(world: EvaluationWorld, user: UUID) -> Iterator[None]:
    with runtime_role(), tenant_context(user, world.organization):
        yield


def candidate(*, purpose: Purpose = Purpose.FRONT_DESK) -> ModelConfiguration:
    for previous in ModelConfiguration.objects.filter(
        purpose=purpose.value, state="active", route="primary"
    ):
        transition_model(configuration_id=previous.pk, state="retired")
    model = propose_model(
        terms=ModelTerms(
            purpose=purpose,
            provider="synthetic",
            model_id="synthetic-v1",
            region="br-synthetic",
            retention_terms="synthetic-no-retention",
            training_terms="synthetic-no-training",
            price=Price(3, 5, 1, 7),
        )
    )
    return transition_model(
        configuration_id=model.pk,
        state="approved",
        approval_ref="synthetic-evaluation-terms",
    )


def review_world(
    *,
    organization: UUID,
    clinic: UUID,
    configuration: ModelConfiguration,
) -> EvaluationWorld:
    users = [
        User.objects.create(
            username="synthetic-evaluation-" + uuid4().hex,
            password=make_password(RBAC_RAW_CREDENTIAL),
        )
        for _ in range(5)
    ]
    with owner_context(organization):
        for user in users[:4]:
            UserClinicRole.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                user=user,
                role="physician",
            )
        for user in (users[0], users[4]):
            UserClinicRole.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                user=user,
                role="owner",
            )
    reviewers = ReviewerAssignment(users[1].pk, users[2].pk, users[3].pk)
    with runtime_role(), tenant_context(users[0].pk, organization):
        protocol = (
            EvaluationProtocol.objects.filter(
                clinic_id=clinic,
                capability=PURPOSE_CAPABILITIES[configuration.purpose],
            )
            .order_by("-version")
            .first()
        )
        if protocol is None:
            protocol = register_protocol(
                clinic_id=clinic,
                capability=PURPOSE_CAPABILITIES[configuration.purpose],
                corpus_version="engineering-v1",
                thresholds=ENGINEERING_THRESHOLDS,
                clinical_owner_ref="synthetic-preregistered-engineering-rubric",
            )
        run = start_run(
            clinic_id=clinic,
            protocol_id=protocol.pk,
            configuration_id=configuration.pk,
            reviewers=reviewers,
        )
    results = evaluate(
        capability=protocol.capability,
        corpus_version=protocol.corpus_version,
        provider_key=configuration.provider,
        model_id=configuration.model_id,
        purpose=Purpose(configuration.purpose),
    )
    with runtime_role(), tenant_context(users[0].pk, organization):
        run = store_results(clinic_id=clinic, run_id=run.pk, results=results)
    return EvaluationWorld(
        organization,
        clinic,
        users[0].pk,
        users[4].pk,
        reviewers,
        configuration,
        protocol,
        run,
    )


def evaluation_world(graph: RbacGraph) -> EvaluationWorld:
    return review_world(
        organization=graph.organization_a,
        clinic=graph.clinic_a,
        configuration=candidate(),
    )


def no_errors(*, correction_ms: int = 1000) -> dict[str, int]:
    return {**dict.fromkeys(ERROR_LABELS, 0), "correction_ms": correction_ms}


def complete_reviews(world: EvaluationWorld) -> EvaluationRun:
    # These deliberately scripted labels prove workflow mechanics only. They
    # are neither a clinician's assessment nor an external threshold approval.
    for user in (world.reviewers.rater_one, world.reviewers.rater_two):
        with as_reviewer(world, user):
            for case in load_corpus("engineering-v1").cases:
                rate_case(
                    clinic_id=world.clinic,
                    run_id=world.run.pk,
                    case_id=case["id"],
                    labels=no_errors(),
                )
    with as_reviewer(world, world.owner):
        return finish_run(clinic_id=world.clinic, run_id=world.run.pk)


def approve_configuration(
    configuration: ModelConfiguration, *, organization: UUID, clinic: UUID
) -> EvaluationWorld:
    """Seed the actual protocol/rating/owner workflow before existing fake models."""
    world = review_world(
        organization=organization,
        clinic=clinic,
        configuration=configuration,
    )
    assert complete_reviews(world).status == "passed"
    with as_reviewer(world, world.owner):
        change = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver):
        decide_change(
            clinic_id=world.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-scripted-engineering-approval",
        )
    return world
