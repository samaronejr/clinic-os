"""Real SQL controls the verdict and activation; fake scoring alone never passes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
)
from apps.ai.evaluation.corpus import load_corpus
from apps.ai.evaluation.protocol import ENGINEERING_THRESHOLDS, EvaluationInputError
from apps.ai.evaluation.report import build_report, report_html
from apps.ai.evaluation.services import (
    authorized_run,
    decide_change,
    finish_run,
    rate_case,
    register_protocol,
    request_change,
    start_run,
)
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.models import EvaluationProtocol, EvaluationRating
from apps.ai.owner import set_global_switch, transition_model
from apps.ai.policy import check_configuration
from apps.providers.lifecycle import VersionInput, propose_version
from django.db import DatabaseError, connection, transaction

from ai.evaluation_support import (
    as_reviewer,
    complete_reviews,
    evaluation_world,
    no_errors,
    review_world,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_each_clinic_requires_its_own_approval_and_supersession_is_tenant_local(
    rbac_graph: RbacGraph,
) -> None:
    one = evaluation_world(rbac_graph)
    two = review_world(
        organization=rbac_graph.organization_b,
        clinic=rbac_graph.clinic_c,
        configuration=one.configuration,
    )
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-clinic-approval-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    assert complete_reviews(one).status == "passed"
    with as_reviewer(one, one.owner):
        change = request_change(clinic_id=one.clinic, run_id=one.run.pk)
    with as_reviewer(one, one.approver):
        decide_change(
            clinic_id=one.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-first-clinic-approval",
        )
    configuration = transition_model(
        configuration_id=one.configuration.pk, state="active"
    )
    with as_reviewer(one, one.owner):
        check_configuration(configuration, one.clinic)
    with as_reviewer(two, two.owner), pytest.raises(GatewayRefusedError):
        check_configuration(configuration, two.clinic)
    assert complete_reviews(two).status == "passed"
    with as_reviewer(two, two.owner):
        change = request_change(clinic_id=two.clinic, run_id=two.run.pk)
    with as_reviewer(two, two.approver):
        decide_change(
            clinic_id=two.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-second-clinic-approval",
        )
        check_configuration(configuration, two.clinic)
    with as_reviewer(one, one.owner):
        register_protocol(
            clinic_id=one.clinic,
            capability=one.protocol.capability,
            corpus_version="engineering-v1",
            thresholds=dict(ENGINEERING_THRESHOLDS, maximum_error_rate_ppm=1),
            clinical_owner_ref="synthetic-first-clinic-supersession",
        )
        with pytest.raises(GatewayRefusedError):
            check_configuration(configuration, one.clinic)
    with as_reviewer(two, two.owner):
        check_configuration(configuration, two.clinic)


def test_two_independent_reviews_and_explicit_owner_change_are_required(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    with as_reviewer(world, world.owner):
        pending = request_change(clinic_id=world.clinic, run_id=world.run.pk)
        with pytest.raises(DatabaseError), transaction.atomic():
            finish_run(clinic_id=world.clinic, run_id=world.run.pk)
    with (
        as_reviewer(world, world.approver),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        decide_change(
            clinic_id=world.clinic,
            change_id=pending.pk,
            decision="approved",
            approval_ref="synthetic-before-review",
        )
    assert complete_reviews(world).status == "passed"
    # A passing run still does not count as the owner's configuration decision.
    with pytest.raises(DatabaseError), transaction.atomic():
        transition_model(configuration_id=world.configuration.pk, state="active")
    with (
        as_reviewer(world, world.owner),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        decide_change(
            clinic_id=world.clinic,
            change_id=pending.pk,
            decision="approved",
            approval_ref="synthetic-self-approval-refused",
        )
    with as_reviewer(world, world.approver):
        decide_change(
            clinic_id=world.clinic,
            change_id=pending.pk,
            decision="approved",
            approval_ref="synthetic-explicit-owner-review",
        )
    with as_reviewer(world, world.owner):
        report = build_report(clinic_id=world.clinic, run_id=world.run.pk)
    assert (
        transition_model(configuration_id=world.configuration.pk, state="active").state
        == "active"
    )
    assert report["cases"] == 12
    assert report["encounters"] == 4
    assert report["human_review_complete"] is True
    assert report["real_world_performance"] is False
    assert report["external_gates_satisfied"] == []
    assert report["thresholds_sha256"] == world.protocol.thresholds_sha256
    html = report_html(report)
    assert "<script" not in html
    assert "http" not in html
    rates = report["rates"]
    assert isinstance(rates, dict)
    assert rates["clinical_error"]["denominator"] == 12


def test_threshold_history_is_immutable_and_new_registration_invalidates_old_run(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    assert complete_reviews(world).status == "passed"
    with (
        owner_context(world.organization),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        EvaluationProtocol.objects.filter(pk=world.protocol.pk).update(
            thresholds_document="{}"
        )
    with as_reviewer(world, world.owner):
        approved = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver):
        assert (
            decide_change(
                clinic_id=world.clinic,
                change_id=approved.pk,
                decision="approved",
                approval_ref="synthetic-before-threshold-supersession",
            ).status
            == "approved"
        )
    with as_reviewer(world, world.owner):
        changed = register_protocol(
            clinic_id=world.clinic,
            capability=world.protocol.capability,
            corpus_version="engineering-v1",
            thresholds=dict(ENGINEERING_THRESHOLDS, maximum_error_rate_ppm=1),
            clinical_owner_ref="synthetic-adversarial-threshold-edit",
        )
        change = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with (
        as_reviewer(world, world.approver),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        decide_change(
            clinic_id=world.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-stale-threshold-approval",
        )
    assert changed.thresholds_sha256 != world.protocol.thresholds_sha256
    with pytest.raises(DatabaseError), transaction.atomic():
        transition_model(configuration_id=world.configuration.pk, state="active")


def test_disagreement_is_blinded_until_both_votes_and_requires_distinct_adjudicator(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    cases = load_corpus("engineering-v1").cases
    first = cases[0]["id"]
    with as_reviewer(world, world.reviewers.rater_one):
        rate_case(
            clinic_id=world.clinic,
            run_id=world.run.pk,
            case_id=first,
            labels=no_errors(),
        )
    with as_reviewer(world, world.reviewers.rater_two):
        assert not EvaluationRating.objects.filter(run=world.run).exists()
    with as_reviewer(world, world.reviewers.adjudicator):
        assert not EvaluationRating.objects.filter(run=world.run).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            rate_case(
                clinic_id=world.clinic,
                run_id=world.run.pk,
                case_id=first,
                labels=no_errors(),
            )
    for user in (world.reviewers.rater_one, world.reviewers.rater_two):
        with as_reviewer(world, user):
            for case in cases:
                if user == world.reviewers.rater_one and case["id"] == first:
                    continue
                labels = no_errors(
                    correction_ms=1000 if user == world.reviewers.rater_one else 3000
                )
                if user == world.reviewers.rater_two and case["id"] == first:
                    labels["negation"] = 1
                rate_case(
                    clinic_id=world.clinic,
                    run_id=world.run.pk,
                    case_id=case["id"],
                    labels=labels,
                )
    with (
        as_reviewer(world, world.owner),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        finish_run(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.reviewers.adjudicator):
        assert (
            EvaluationRating.objects.filter(run=world.run, case_id=first).count() == 2
        )
        rate_case(
            clinic_id=world.clinic,
            run_id=world.run.pk,
            case_id=first,
            labels=no_errors(),
        )
    with as_reviewer(world, world.owner):
        assert (
            finish_run(clinic_id=world.clinic, run_id=world.run.pk).status == "passed"
        )
        report = build_report(clinic_id=world.clinic, run_id=world.run.pk)
    assert report["adjudicated_disagreements"] == 1
    assert report["kappa"] == 0
    assert report["correction_ms"] == {
        "basis": "mean_of_two_independent_raters",
        "mean": 2000,
        "cluster_bootstrap_95": (2000, 2000),
    }


@pytest.mark.parametrize(
    "error",
    ["critical_error", "unauthorized_effect", "duplicate_effect", "missed_escalation"],
)
def test_observed_critical_or_forbidden_effect_fails_even_with_perfect_schema(
    rbac_graph: RbacGraph, error: str
) -> None:
    world = evaluation_world(rbac_graph)
    for user in (world.reviewers.rater_one, world.reviewers.rater_two):
        with as_reviewer(world, user):
            for index, case in enumerate(load_corpus("engineering-v1").cases):
                labels = no_errors()
                labels[error] = int(index == 0)
                rate_case(
                    clinic_id=world.clinic,
                    run_id=world.run.pk,
                    case_id=case["id"],
                    labels=labels,
                )
    with as_reviewer(world, world.owner):
        assert (
            finish_run(clinic_id=world.clinic, run_id=world.run.pk).status == "failed"
        )
        change = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with (
        as_reviewer(world, world.approver),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        decide_change(
            clinic_id=world.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-failed-run",
        )


def test_assignments_scope_and_raw_verdict_writes_cannot_be_forged(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    with as_reviewer(world, world.owner):
        with pytest.raises(DatabaseError), transaction.atomic():
            start_run(
                clinic_id=world.clinic,
                protocol_id=world.protocol.pk,
                configuration_id=world.configuration.pk,
                reviewers=replace(world.reviewers, rater_two=world.reviewers.rater_one),
            )
        for slot in ("rater_one", "rater_two", "adjudicator"):
            with pytest.raises(DatabaseError), transaction.atomic():
                start_run(
                    clinic_id=world.clinic,
                    protocol_id=world.protocol.pk,
                    configuration_id=world.configuration.pk,
                    reviewers=replace(world.reviewers, **{slot: world.owner}),
                )
        with (
            connection.cursor() as cursor,
            pytest.raises(DatabaseError),
            transaction.atomic(),
        ):
            cursor.execute(
                "UPDATE clinic_app.ai_evaluationrun SET status='passed' WHERE id=%s",
                [world.run.pk],
            )
        with pytest.raises(GatewayAccessDeniedError):
            authorized_run(clinic_id=rbac_graph.clinic_c, run_id=world.run.pk)
        with pytest.raises(GatewayAccessDeniedError):
            authorized_run(clinic_id=world.clinic, run_id=uuid4())
    with (
        as_reviewer(world, world.reviewers.rater_one),
        pytest.raises(GatewayAccessDeniedError),
    ):
        register_protocol(
            clinic_id=world.clinic,
            capability=world.protocol.capability,
            corpus_version="engineering-v1",
            thresholds=ENGINEERING_THRESHOLDS,
            clinical_owner_ref="synthetic-no-owner-role",
        )


@pytest.mark.parametrize(
    "source_path",
    [
        None,
        "apps/ehr/chart_sources.py",
        "apps/orders/chart_sources.py",
        "apps/orders/queries.py",
        "apps/workflows/chart_sources.py",
    ],
    ids=["prompt", "ehr-reader", "result-reader", "result-query", "task-reader"],
)
def test_current_source_drift_refuses_approval_activation_and_admission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    source_path: str | None,
) -> None:
    world = evaluation_world(rbac_graph)
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-reader-drift-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    complete_reviews(world)
    with as_reviewer(world, world.owner):
        approved = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver):
        decide_change(
            clinic_id=world.clinic,
            change_id=approved.pk,
            decision="approved",
            approval_ref="synthetic-before-source-change",
        )
    configuration = transition_model(
        configuration_id=world.configuration.pk, state="active"
    )
    with as_reviewer(world, world.owner):
        check_configuration(configuration, world.clinic)
    before = candidate_versions(configuration)

    def changed_prompt(purpose: object) -> tuple[str, str]:
        return "Synthetic changed prompt", "f" * 64

    if source_path is None:
        monkeypatch.setattr("apps.ai.evaluation.versions.load_prompt", changed_prompt)
    else:
        original_read = Path.read_bytes
        reader = Path(__file__).resolve().parents[2] / source_path
        assert reader.is_file()

        def changed_reader(path: Path) -> bytes:
            content = original_read(path)
            if path == reader:
                return content + b"\n# Changed retrieval selection.\n"
            return content

        monkeypatch.setattr(Path, "read_bytes", changed_reader)
    after = candidate_versions(configuration)
    assert before["model"] == after["model"]
    assert before["schema"] == after["schema"]
    assert (
        before["retrieval" if source_path else "prompt"]
        != after["retrieval" if source_path else "prompt"]
    )
    with as_reviewer(world, world.owner), pytest.raises(GatewayRefusedError):
        check_configuration(configuration, world.clinic)
    with as_reviewer(world, world.owner):
        change = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver), pytest.raises(EvaluationInputError):
        decide_change(
            clinic_id=world.clinic,
            change_id=change.pk,
            decision="approved",
            approval_ref="synthetic-stale-source",
        )
    with pytest.raises((DatabaseError, GatewayInputError)), transaction.atomic():
        transition_model(configuration_id=world.configuration.pk, state="active")
