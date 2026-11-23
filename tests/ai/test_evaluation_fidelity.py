"""Regressions at the boundary between an engineering observation and its claim."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import partial
from gettext import GNUTranslations
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.ai import front_desk
from apps.ai.contracts import GatewayInputError, Purpose
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.ai.evaluation.report import build_report, report_html
from apps.ai.evaluation.runner import evaluate, score_run
from apps.ai.evaluation.services import (
    PURPOSE_CAPABILITIES,
    start_run,
    start_stage_run,
    store_results,
)
from apps.ai.evaluation.versions import source_versions
from apps.ai.extraction_documents import recorded_document_extraction
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import DatabaseError, connection, transaction

from ai.evaluation_support import (
    as_reviewer,
    candidate,
    complete_reviews,
    evaluation_world,
    review_world,
)
from ai.test_rollout import approve_stage

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


@pytest.mark.parametrize(
    "purpose",
    [
        Purpose.SCRIBE_ASR,
        Purpose.SCRIBE_DRAFT,
        Purpose.EXTRACTION,
        Purpose.CARE_COORDINATION,
        Purpose.REVENUE_CYCLE,
        Purpose.ANALYST,
    ],
)
def test_samples_marked_valid_are_domain_outputs(purpose: Purpose) -> None:
    result = evaluate(
        capability=PURPOSE_CAPABILITIES[purpose.value],
        purpose=purpose,
        corpus_version="engineering-v1",
        provider_key="synthetic",
        model_id="synthetic-v1",
    )
    cases = result["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 12
    # All six production parsers require a JSON object. An ordinary generic
    # completion or extraction string is not one, regardless of token usage.
    assert all(
        case["schema_valid"] and case["output"].startswith("{") for case in cases
    )


def test_evaluation_scores_the_recorded_ocr_against_its_reviewed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = partial(
        evaluate,
        capability="AI-03",
        purpose=Purpose.EXTRACTION,
        corpus_version="engineering-v1",
        provider_key="recorded_fixture",
        model_id="extraction-fixture-v1",
    )
    result = replay()
    assert result["automatic_failures"] == 0
    cases = result["cases"]
    assert isinstance(cases, list)
    assert all(len(json.loads(case["output"])["candidates"]) == 2 for case in cases)

    def substituted_unit(content: str) -> str:
        reply = json.loads(recorded_document_extraction(content))
        row = reply["candidates"][0]
        row["unit"] = "g/dL"
        row["raw_text"] = row["raw_text"].replace("mg/dL", "g/dL")
        return json.dumps(reply)

    monkeypatch.setattr(
        "apps.ai.adapters.recorded_document_extraction", substituted_unit
    )
    assert replay()["automatic_failures"] == 12


@pytest.mark.parametrize(
    ("purpose", "module", "entry"),
    [
        (Purpose.SCRIBE_ASR, "apps.scribe.asr", "parse_batch"),
        (Purpose.SCRIBE_DRAFT, "apps.scribe.draft_contracts", "parse_suggestions"),
        (Purpose.EXTRACTION, "apps.ai.extraction_contracts", "parse_proposals"),
        (Purpose.BRIEF, "apps.ai.brief_contracts", "validate_reply"),
        (
            Purpose.DECISION_SUPPORT,
            "apps.ai.decision_contracts",
            "validate_decision_reply",
        ),
        (Purpose.CARE_COORDINATION, "apps.ai.care_contracts", "validate_reply"),
        (Purpose.REVENUE_CYCLE, "apps.ai.revenue_contracts", "validate_reply"),
        (Purpose.ANALYST, "apps.analytics.analyst_contracts", "validate_reply"),
    ],
)
def test_production_parser_refusal_is_an_observed_automatic_failure(
    purpose: Purpose, module: str, entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object, **kwargs: object) -> None:
        raise GatewayInputError

    monkeypatch.setattr(import_module(module), entry, refused)
    report = evaluate(
        capability=PURPOSE_CAPABILITIES[purpose.value],
        purpose=purpose,
        corpus_version="engineering-v1",
        provider_key="synthetic",
        model_id="synthetic-v1",
    )
    assert report["automatic_failures"] == 12


def test_front_desk_wrong_handoff_fails_even_when_the_reply_is_well_formed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def greeting(text: str, *, challenge_pending: bool, offer_pending: bool) -> str:
        return "greeting"

    monkeypatch.setattr(front_desk, "classify", greeting)
    report = evaluate(
        capability="AI-04",
        purpose=Purpose.FRONT_DESK,
        corpus_version="engineering-v1",
        provider_key="synthetic",
        model_id="synthetic-v1",
    )
    assert report["automatic_failures"] == 9
    assert report["tool_calls"] == 0


@pytest.mark.parametrize("other_purpose", [Purpose.SCRIBE_DRAFT, Purpose.SCRIBE_ASR])
@pytest.mark.django_db(transaction=True)
def test_genuine_samples_cannot_be_reattached_to_another_run(
    rbac_graph: RbacGraph,
    other_purpose: Purpose,
) -> None:
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=candidate(purpose=Purpose.SCRIBE_ASR),
    )
    other = candidate(purpose=other_purpose)
    with as_reviewer(world, world.owner):
        samples = world.run.sample_results
        run = start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=other.pk,
            reviewers=world.reviewers,
        )
        with pytest.raises(EvaluationInputError):
            store_results(clinic_id=world.clinic, run_id=run.pk, results=samples)
        assert other.pk != world.run.configuration_id
        for item in samples["cases"]:
            assert item["origin"]["run_id"] == str(world.run.pk)
            assert item["origin"]["configuration_id"] == str(world.configuration.pk)
            assert item["origin"]["versions"] == world.run.versions
            assert (
                item["origin"]["thresholds_sha256"] == world.protocol.thresholds_sha256
            )
            assert len(item["origin"]["protocol_sha256"]) == 64
        # The authenticated ciphertext of the other run cannot be copied into
        # the native writer even without going through the Python service.
        with (
            pytest.raises(DatabaseError, match="invalid evaluation origin"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.ai_eval_store(%s,%s,sample_results,%s) FROM "
                "clinic_app.ai_evaluationrun WHERE id=%s",
                [
                    world.clinic,
                    run.pk,
                    secret_store().get_secret(KEK_SECRET_NAME),
                    world.run.pk,
                ],
            )


@pytest.mark.django_db(transaction=True)
def test_shadow_rejects_offline_samples_and_forged_counts(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    approved = approve_stage(world)
    with as_reviewer(world, world.owner):
        shadow = start_stage_run(
            clinic_id=world.clinic, change_id=approved.pk, reviewers=world.reviewers
        )
        with pytest.raises(EvaluationInputError):
            store_results(
                clinic_id=world.clinic,
                run_id=shadow.pk,
                results=world.run.sample_results,
            )
    genuine = score_run(shadow)
    tampered = deepcopy(genuine)
    observations = tampered["cases"]
    assert isinstance(observations, list)
    observations[0]["schema_valid"] = False
    with as_reviewer(world, world.owner):
        with pytest.raises(EvaluationInputError):
            store_results(clinic_id=world.clinic, run_id=shadow.pk, results=tampered)
        packet = protect(
            purpose="ai.evaluation.samples.v1", plaintext=json.dumps(tampered).encode()
        )
        with (
            pytest.raises(DatabaseError, match="invalid evaluation count"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.ai_eval_store(%s,%s,%s,%s)",
                [
                    world.clinic,
                    shadow.pk,
                    packet,
                    secret_store().get_secret(KEK_SECRET_NAME),
                ],
            )
        stored = store_results(
            clinic_id=world.clinic, run_id=shadow.pk, results=genuine
        )
        assert stored.automatic_failures == 0
        assert stored.sample_results["cases"][0]["origin"]["stage"] == "shadow"


@pytest.mark.django_db(transaction=True)
def test_standalone_scripted_report_never_attests_human_review(
    rbac_graph: RbacGraph,
) -> None:
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=candidate(),
    )
    assert complete_reviews(world).status == "passed"
    with as_reviewer(world, world.owner):
        report = build_report(clinic_id=world.clinic, run_id=world.run.pk)
    assert report["human_review_complete"] is False
    assert report["vote_workflow_complete"] is True
    assert report["review_kind"] == "scripted_engineering"
    assert "Votos simulados" in report_html(report)


def test_compiled_patient_copy_alone_invalidates_front_desk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_bytes
    before = source_versions(Purpose.FRONT_DESK)

    def translated(path: Path) -> bytes:
        content = original(path)
        if path.name != "django.mo":
            return content
        actual = GNUTranslations(BytesIO(content)).gettext(front_desk.SAFETY)
        changed = content.replace(
            actual.encode(), actual.removesuffix(".").encode() + b"!"
        )
        assert GNUTranslations(BytesIO(changed)).gettext(front_desk.SAFETY) != actual
        return changed

    monkeypatch.setattr(Path, "read_bytes", translated)
    assert source_versions(Purpose.FRONT_DESK) != before
