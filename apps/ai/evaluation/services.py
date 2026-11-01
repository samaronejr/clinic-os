"""Clinic-scoped human authority for preregistration, blinded reviews and changes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.evaluation.corpus import CAPABILITIES, load_corpus
from apps.ai.evaluation.protocol import (
    EvaluationInputError,
    threshold_document,
    threshold_hash,
    validate_labels,
)
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.models import (
    EvaluationProtocol,
    EvaluationRating,
    EvaluationRun,
    ModelChangeRequest,
    ModelConfiguration,
)
from apps.audit.services import record_phase1_event
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from collections.abc import Mapping
type ReviewSlot = Literal["rater_one", "rater_two", "adjudicator"]
type ManagementMode = Literal["owner", "clinical_owner", "clinician"]
PURPOSE_CAPABILITIES = {
    "scribe_asr": "AI-01",
    "scribe_draft": "AI-01",
    "brief": "AI-02",
    "extraction": "AI-03",
    "front_desk": "AI-04",
    "analyst": "AI-07",
    "decision_support": "AI-08",
}


@dataclass(frozen=True, slots=True)
class ReviewerAssignment:
    """Bind two independent raters and a distinct adjudicator before scoring."""

    rater_one: UUID
    rater_two: UUID
    adjudicator: UUID


def evaluation_staff(*, clinic_id: UUID, mode: ManagementMode) -> UUID:
    """Ask the deployed role predicate before looking up any requested record."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_user_id',true),'')::uuid "
            "WHERE clinic_app.ai_eval_staff(%s,%s)",
            [clinic_id, mode],
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], UUID):
        raise GatewayAccessDeniedError
    return row[0]


@transaction.atomic
def register_protocol(
    *,
    clinic_id: UUID,
    capability: str,
    corpus_version: str,
    thresholds: Mapping[str, int],
    clinical_owner_ref: str,
) -> EvaluationProtocol:
    """Freeze corpus, sample sizes and thresholds before a candidate can be scored."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="clinical_owner")
    if (
        capability not in CAPABILITIES
        or re.fullmatch(r"synthetic-[a-z0-9-]{1,112}", clinical_owner_ref) is None
    ):
        raise EvaluationInputError
    corpus = load_corpus(corpus_version)
    document = threshold_document(thresholds)
    clinic = Clinic.objects.get(pk=clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-eval-protocol:{clinic_id}:{capability}"],
        )
    previous = EvaluationProtocol.objects.filter(
        clinic_id=clinic_id, capability=capability
    ).aggregate(latest=Max("version"))["latest"]
    protocol = EvaluationProtocol.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        capability=capability,
        version=(previous or 0) + 1,
        corpus_version=corpus_version,
        corpus_sha256=corpus.sha256,
        case_manifest=[
            {
                key: case[key]
                for key in (
                    "id",
                    "encounter",
                    "specialty",
                    "device",
                    "accent",
                    "adversarial",
                )
            }
            for case in corpus.cases
        ],
        thresholds_document=document,
        thresholds_sha256=threshold_hash(thresholds),
        registered_by_id=actor,
        clinical_owner_ref=clinical_owner_ref,
    )
    record_phase1_event(
        "ai.evaluation.registered", clinic_id=clinic_id, affected_record_id=protocol.pk
    )
    return protocol


def authorized_run(*, clinic_id: UUID, run_id: UUID) -> EvaluationRun:
    """RLS gives the same empty result for unknown, foreign and unassigned runs."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_eval_run_visible(%s)", [run_id])
        if cursor.fetchone() != (True,):
            raise GatewayAccessDeniedError
    run = (
        EvaluationRun.objects.filter(pk=run_id, clinic_id=clinic_id)
        .select_related("protocol", "configuration")
        .first()
    )
    if run is None:
        raise GatewayAccessDeniedError
    return run


@transaction.atomic
def start_run(
    *,
    clinic_id: UUID,
    protocol_id: UUID,
    configuration_id: UUID,
    reviewers: ReviewerAssignment,
) -> EvaluationRun:
    """Allocate an immutable candidate and three distinct current clinicians."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="clinical_owner")
    protocol = EvaluationProtocol.objects.filter(
        pk=protocol_id, clinic_id=clinic_id
    ).first()
    configuration = ModelConfiguration.objects.filter(pk=configuration_id).first()
    if protocol is None or configuration is None:
        raise GatewayAccessDeniedError
    corpus = load_corpus(protocol.corpus_version)
    versions = candidate_versions(configuration)
    if (
        corpus.sha256 != protocol.corpus_sha256
        or PURPOSE_CAPABILITIES.get(configuration.purpose) != protocol.capability
        or configuration.state == "retired"
        or versions["prompt"] != configuration.prompt_sha256
        or versions["retrieval"] != configuration.retrieval_sha256
        or versions["schema"] != configuration.schema_sha256
    ):
        raise EvaluationInputError
    run = EvaluationRun.objects.create(
        organization_id=protocol.organization_id,
        clinic_id=clinic_id,
        protocol=protocol,
        configuration=configuration,
        configuration_version=configuration.version,
        versions=versions,
        rater_one_id=reviewers.rater_one,
        rater_two_id=reviewers.rater_two,
        adjudicator_id=reviewers.adjudicator,
        created_by_id=actor,
    )
    record_phase1_event(
        "ai.evaluation.started", clinic_id=clinic_id, affected_record_id=run.pk
    )
    return run


@transaction.atomic
def store_results(
    *, clinic_id: UUID, run_id: UUID, results: dict[str, object]
) -> EvaluationRun:
    """Retain one offline result after the caller has left its provider transaction."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="clinical_owner")
    run = authorized_run(clinic_id=clinic_id, run_id=run_id)
    if run.created_by_id != actor:
        raise GatewayAccessDeniedError
    corpus = load_corpus(run.protocol.corpus_version)
    cases = results.get("cases")
    failures = results.get("automatic_failures")
    if (
        run.scored_at is not None
        or run.status != "pending"
        or candidate_versions(run.configuration) != run.versions
        or corpus.sha256 != run.protocol.corpus_sha256
        or results.get("corpus_sha256") != corpus.sha256
        or results.get("capability") != run.protocol.capability
        or results.get("data_class") != "synthetic"
        or results.get("real_world_performance") is not False
        or results.get("human_review_complete") is not False
        or results.get("tool_calls") != 0
        or not isinstance(cases, list)
        or [item.get("id") for item in cases if isinstance(item, dict)]
        != [case["id"] for case in corpus.cases]
        or type(failures) is not int
        or not 0 <= failures <= len(corpus.cases)
    ):
        raise EvaluationInputError
    run.sample_results = results
    run.automatic_failures = failures
    run.scored_at = timezone.now()
    run.save(update_fields=("sample_results", "automatic_failures", "scored_at"))
    record_phase1_event(
        "ai.evaluation.scored", clinic_id=clinic_id, affected_record_id=run.pk
    )
    return run


def review_slot(run: EvaluationRun, actor: UUID) -> ReviewSlot:
    """Only one preassigned slot is ever representable for a human."""
    if actor == run.rater_one_id:
        return "rater_one"
    if actor == run.rater_two_id:
        return "rater_two"
    if actor == run.adjudicator_id:
        return "adjudicator"
    raise GatewayAccessDeniedError


@transaction.atomic
def rate_case(
    *, clinic_id: UUID, run_id: UUID, case_id: str, labels: Mapping[str, int]
) -> EvaluationRating:
    """Persist one blinded vote; the SQL trigger enforces independent ordering."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="clinician")
    run = authorized_run(clinic_id=clinic_id, run_id=run_id)
    rating = EvaluationRating.objects.create(
        organization_id=run.organization_id,
        clinic_id=clinic_id,
        run=run,
        case_id=case_id,
        slot=review_slot(run, actor),
        user_id=actor,
        labels=validate_labels(dict(labels)),
    )
    record_phase1_event(
        "ai.evaluation.rated", clinic_id=clinic_id, affected_record_id=rating.pk
    )
    return rating


@transaction.atomic
def finish_run(*, clinic_id: UUID, run_id: UUID) -> EvaluationRun:
    """Use the resolver's human-label verdict instead of caller-supplied scores."""
    evaluation_staff(clinic_id=clinic_id, mode="clinical_owner")
    run = authorized_run(clinic_id=clinic_id, run_id=run_id)
    if run.status != "pending":
        return run
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_eval_finalize(%s,%s)", [clinic_id, run_id])
        outcome = cursor.fetchone()
    if outcome is None or outcome[0] not in {"passed", "failed"}:
        raise EvaluationInputError
    record_phase1_event(
        f"ai.evaluation.{outcome[0]}", clinic_id=clinic_id, affected_record_id=run.pk
    )
    return authorized_run(clinic_id=clinic_id, run_id=run_id)


@transaction.atomic
def request_change(*, clinic_id: UUID, run_id: UUID) -> ModelChangeRequest:
    """Request a change without activating even a configuration whose run passed."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="owner")
    run = authorized_run(clinic_id=clinic_id, run_id=run_id)
    change = ModelChangeRequest.objects.create(
        organization_id=run.organization_id,
        clinic_id=clinic_id,
        configuration_id=run.configuration_id,
        run=run,
        requested_by_id=actor,
    )
    record_phase1_event(
        "ai.evaluation.change_requested",
        clinic_id=clinic_id,
        affected_record_id=change.pk,
    )
    return change


@transaction.atomic
def decide_change(
    *,
    clinic_id: UUID,
    change_id: UUID,
    decision: Literal["approved", "rejected"],
    approval_ref: str,
) -> ModelChangeRequest:
    """Approval binds current code and an unsuperseded preregistration."""
    actor = evaluation_staff(clinic_id=clinic_id, mode="owner")
    change = (
        ModelChangeRequest.objects.filter(pk=change_id, clinic_id=clinic_id)
        .select_related("configuration", "run")
        .first()
    )
    if change is None:
        raise GatewayAccessDeniedError
    if (
        decision not in {"approved", "rejected"}
        or candidate_versions(change.configuration) != change.run.versions
        or re.fullmatch(r"synthetic-[a-z0-9-]{1,112}", approval_ref) is None
    ):
        raise EvaluationInputError
    change.status = decision
    change.approved_by_id = actor
    change.approval_ref = approval_ref
    change.decided_at = timezone.now()
    change.save(update_fields=("status", "approved_by", "approval_ref", "decided_at"))
    record_phase1_event(
        f"ai.evaluation.change_{decision}",
        clinic_id=clinic_id,
        affected_record_id=change.pk,
    )
    return change


def thresholds_for(run: EvaluationRun) -> dict[str, int]:
    """Read only the frozen document, never a mutable current threshold setting."""
    return cast("dict[str, int]", json.loads(run.protocol.thresholds_document))
