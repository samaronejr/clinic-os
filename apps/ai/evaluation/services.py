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

from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.ai.evaluation.corpus import CAPABILITIES, load_corpus
from apps.ai.evaluation.origin import (
    PURPOSE_CAPABILITIES,
    domain_protocol_hash,
    run_origin,
)
from apps.ai.evaluation.protocol import (
    EvaluationInputError,
    threshold_document,
    threshold_hash,
    validate_labels,
)
from apps.ai.evaluation.rollout import (
    STAGES,
    autonomy_allowed,
    rollout_status,
    stop_rollout,
)
from apps.ai.evaluation.runner import verify_receipt
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.models import (
    EvaluationProtocol,
    EvaluationRating,
    EvaluationRun,
    GatewaySwitch,
    ModelChangeRequest,
    ModelConfiguration,
)
from apps.audit.services import record_phase1_event
from apps.core.secrets import secret_store
from apps.identity.models import Clinic
from apps.tenancy.envelope import KEK_SECRET_NAME, protect

__all__ = (
    "PURPOSE_CAPABILITIES",
    "ReviewerAssignment",
    "authorized_run",
    "autonomy_allowed",
    "decide_change",
    "evaluation_staff",
    "finish_run",
    "rate_case",
    "register_protocol",
    "request_change",
    "review_slot",
    "rollout_status",
    "start_run",
    "start_stage_run",
    "stop_rollout",
    "store_results",
    "thresholds_for",
)

if TYPE_CHECKING:
    from collections.abc import Mapping
type ReviewSlot = Literal["rater_one", "rater_two", "adjudicator"]
type ManagementMode = Literal["owner", "clinical_owner", "clinician"]


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
        domain_protocol_sha256=domain_protocol_hash(),
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
    return _start_run(
        clinic_id=clinic_id,
        protocol_id=protocol_id,
        configuration_id=configuration_id,
        reviewers=reviewers,
        approval=None,
    )


@transaction.atomic
def start_stage_run(
    *, clinic_id: UUID, change_id: UUID, reviewers: ReviewerAssignment
) -> EvaluationRun:
    """Collect isolated evidence under one current, explicitly approved exposure."""
    status = rollout_status(clinic_id=clinic_id, change_id=change_id)
    if status not in STAGES or status == "offline":
        raise GatewayRefusedError(reason="configuration_unavailable")
    approval = ModelChangeRequest.objects.select_related("run").get(
        pk=change_id, clinic_id=clinic_id
    )
    return _start_run(
        clinic_id=clinic_id,
        protocol_id=approval.run.protocol_id,
        configuration_id=approval.configuration_id,
        reviewers=reviewers,
        approval=approval,
    )


def _start_run(
    *,
    clinic_id: UUID,
    protocol_id: UUID,
    configuration_id: UUID,
    reviewers: ReviewerAssignment,
    approval: ModelChangeRequest | None,
) -> EvaluationRun:
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
        or protocol.domain_protocol_sha256 != domain_protocol_hash()
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
        stage=approval.stage if approval is not None else "offline",
        rollout_approval=approval,
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
    origin = run_origin(run)
    if (
        run.scored_at is not None
        or not verify_receipt(results)
        or run.protocol.domain_protocol_sha256 != domain_protocol_hash()
        or results.get("origin") != origin
        or results.get("domain_protocol_sha256") != run.protocol.domain_protocol_sha256
        or results.get("corpus_version") != run.protocol.corpus_version
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
        or len(cases) != len(corpus.cases)
        or any(
            not isinstance(item, dict)
            or item.get("origin") != origin
            or type(item.get("schema_valid")) is not bool
            or any(item.get(key) != value for key, value in case.items())
            for item, case in zip(cases, corpus.cases, strict=True)
        )
        or type(failures) is not int
        or failures != sum(not item["schema_valid"] for item in cases)
    ):
        raise EvaluationInputError
    envelope = protect(
        purpose="ai.evaluation.samples.v1",
        plaintext=json.dumps(
            results, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode(),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_eval_store(%s,%s,%s,%s)",
            [clinic_id, run.pk, envelope, secret_store().get_secret(KEK_SECRET_NAME)],
        )
    run.refresh_from_db()
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
    *,
    clinic_id: UUID,
    run_id: UUID,
    case_id: str,
    labels: Mapping[str, int],
    review_source: Literal["interactive", "scripted", "unattested"] = "unattested",
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
        review_source=review_source,
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
    if run.stage not in STAGES:
        raise EvaluationInputError
    stage, exposure = STAGES[run.stage]
    control = GatewaySwitch.objects.filter(pk=1).first()
    change = ModelChangeRequest.objects.create(
        organization_id=run.organization_id,
        clinic_id=clinic_id,
        configuration_id=run.configuration_id,
        run=run,
        requested_by_id=actor,
        stage=stage,
        exposure=exposure,
        switch_generation=control.generation if control is not None else 0,
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
    if decision == "approved":
        record_phase1_event(
            f"ai.rollout.{change.stage}",
            clinic_id=clinic_id,
            affected_record_id=change.pk,
        )
    return change


def thresholds_for(run: EvaluationRun) -> dict[str, int]:
    """Read only the frozen document, never a mutable current threshold setting."""
    return cast("dict[str, int]", json.loads(run.protocol.thresholds_document))
