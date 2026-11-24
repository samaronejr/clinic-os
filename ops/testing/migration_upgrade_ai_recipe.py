"""Pure fixture declarations consumed by the writer and the independent oracle."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from django.db.models import Model

_CONTRACT = run_path(str(Path(__file__).with_name("migration_upgrade_ai_fixture.py")))


class FixtureRows(Protocol):
    """Symbolic or persisted row backend for the same declarations."""

    @property
    def clinic(self) -> UUID:
        """Return the declared clinic identity."""

    def make(self, label: str, **fields: object) -> Model:
        """Declare a row and return its reference."""

    def find(self, label: str, **fields: object) -> Model:
        """Resolve one earlier declaration."""

    def now(self) -> datetime:
        """Provide the fixture clock."""

    def uuid(self) -> UUID:
        """Allocate a generated value."""


def _invocations(
    fixtures: FixtureRows,
    configuration: Model,
    purpose: str,
    user: UUID,
    enrollment_id: object,
) -> dict[str, Model]:
    now = fixtures.now()
    future = now + timedelta(days=30)
    digest = "a" * 64
    result: dict[str, Model] = {}
    for status in _CONTRACT["STATUSES"]:
        source = fixtures.make(
            "ai.AIArtifact",
            user_id=user,
            purpose=purpose,
            patient_enrollment_id=(
                None if purpose in {"front_desk", "revenue_cycle"} else enrollment_id
            ),
            kind="input",
            method=(
                "transcribe"
                if purpose == "scribe_asr"
                else "extract"
                if purpose == "extraction"
                else "complete"
            ),
            content=_CONTRACT["INPUT_TEMPLATE"].format(purpose=purpose, status=status),
            retention_class="transient_input",
            expires_at=future,
        )
        operation_id = fixtures.uuid()
        invocation_id = fixtures.uuid()
        fixtures.make(
            "comms.IntegrationOperation",
            id=operation_id,
            actor_id=user,
            kind="action",
            channel="",
            payload_digest=digest,
            provider="ai-gateway",
            subject_type="ai.invocation",
            subject_id=invocation_id,
            idempotency_key=fixtures.uuid(),
        )
        invocation = fixtures.make(
            "ai.AIInvocation",
            id=invocation_id,
            user_id=user,
            purpose=purpose,
            configuration=configuration,
            config_version=1,
            prompt_sha256=digest,
            inputs_ref=source,
            operation_id=operation_id,
            limit_tokens_in=10,
            limit_tokens_out=10,
            limit_audio_seconds=10,
            reserved_micros=100,
            status=status,
            settled_at=now if status == "succeeded" else None,
        )
        result[purpose + ":" + status] = invocation
        if status == "succeeded":
            fixtures.make(
                "ai.AIArtifact",
                user_id=user,
                purpose=purpose,
                patient_enrollment_id=(
                    None
                    if purpose in {"front_desk", "revenue_cycle"}
                    else enrollment_id
                ),
                kind="output",
                method=(
                    "transcribe"
                    if purpose == "scribe_asr"
                    else "extract"
                    if purpose == "extraction"
                    else "complete"
                ),
                invocation=invocation,
                content=_CONTRACT["OUTPUT_TEMPLATE"].format(
                    purpose=purpose, status=status
                ),
                retention_class="review_draft",
                expires_at=future,
            )
    return result


def _transcription(
    fixtures: FixtureRows,
    markers: Mapping[str, object],
    enrollment_id: object,
    invocation: Model,
) -> dict[str, str]:
    patient = UUID(str(markers["patient_id"]))
    owner = UUID(str(markers["owner_id"]))
    physician = UUID(str(markers["practitioner_id"]))
    now = fixtures.now()
    future = now + timedelta(days=30)
    digest = "a" * 64
    fixtures.make(
        "identity.ProfessionalRegistration",
        user_id=physician,
        role="physician",
        council="CRM",
        jurisdiction="SP",
        number="SYNTHETIC",
        status="regular",
        synthetic=True,
        valid_from=now - timedelta(days=1),
        valid_to=future,
    )
    fixtures.make(
        "identity.CareTeamMembership",
        patient_enrollment_id=enrollment_id,
        user_id=physician,
        role="physician",
        valid_from=now - timedelta(days=1),
        valid_to=future,
    )
    grant = fixtures.make(
        "intake.PatientAccessGrant",
        patient_id=patient,
        enrollment_id=enrollment_id,
        issued_by_id=owner,
        issued_by_label="Synthetic owner",
        secret_hash=bytes.fromhex(digest),
        operations=["consent"],
        expires_at=future,
    )
    session = fixtures.make(
        "intake.PatientSession",
        grant=grant,
        patient_id=patient,
        enrollment_id=enrollment_id,
        operations=["consent"],
        expires_at=future,
        idle_expires_at=future,
    )
    text = fixtures.make(
        "consent.ConsentText",
        purpose="ai_assistance",
        version=1,
        text="Synthetic consent.",
        digest=digest,
        published_by_id=owner,
    )
    consent = fixtures.make(
        "consent.ConsentAcceptance",
        patient_id=patient,
        enrollment_id=enrollment_id,
        text=text,
        patient_session=session,
        accepted_at=now,
    )
    encounter = fixtures.make(
        "ehr.Encounter",
        appointment_id=markers["appointment_id"],
        patient_id=patient,
        physician_id=physician,
    )
    fixtures.make(
        "consent.AIUseDisclosure",
        encounter=encounter,
        patient_id=patient,
        informed=True,
        refused=False,
        recorded_by_id=physician,
        recorded_at=now,
    )
    recording_text = fixtures.make(
        "consent.ConsentText",
        purpose="consultation_recording",
        version=1,
        text="Synthetic recording consent.",
        digest=digest,
        published_by_id=owner,
    )
    recording_consent = fixtures.make(
        "consent.ConsentAcceptance",
        patient_id=patient,
        enrollment_id=enrollment_id,
        text=recording_text,
        patient_session=session,
        accepted_at=now,
    )
    recording = fixtures.make(
        "scribe.RecordingSession",
        encounter=encounter,
        enrollment_id=enrollment_id,
        physician_id=physician,
        consent=recording_consent,
        capability_version_id=fixtures.uuid(),
        capability_snapshot={"synthetic": True},
        token_digest=digest,
        creation_key=fixtures.uuid(),
        state="recording",
        epoch=1,
    )
    epoch = fixtures.make("scribe.CaptureEpoch", session=recording, number=1)
    chunk = fixtures.make(
        "scribe.AudioChunk",
        session=recording,
        epoch=epoch,
        track="microphone",
        seq=0,
        sha256=digest,
        duration_ms=1000,
        codec="audio/webm;codecs=opus",
        size_bytes=1,
        storage_key=fixtures.uuid().hex,
        wrapped_key=b"synthetic",
        encrypted_data=b"synthetic",
        expires_at=future,
        state="accepted",
    )
    request = fixtures.make(
        "scribe.ChunkTranscription",
        session=recording,
        chunk=chunk,
        consent=consent,
        invocation=invocation,
    )
    source_id = fixtures.uuid()
    coverage = {
        "slots": [],
        "completed": [str(chunk.pk)],
        "sources": [str(source_id)],
        "recording_closed": False,
    }
    previous = None
    for index in range(2):
        version = fixtures.make(
            "scribe.TranscriptVersion",
            session=recording,
            predecessor=previous,
            revision=index + 1,
            state="partial",
            mutation="recognized",
            coverage=coverage,
            request=request,
            recognition_index=index,
            created_by_id=physician,
        )
        fixtures.make(
            "scribe.TranscriptSegment",
            session=recording,
            version=version,
            source_id=source_id,
            chunk=chunk,
            start_ms=0,
            end_ms=1000,
            text="Synthetic transcript, no clinical content.",
            speaker="unknown",
            confidence_milli=900,
            uncertainty=[],
            language="pt-BR",
        )
        previous = version
    if previous is None:
        message = "synthetic transcript fixture is empty"
        raise RuntimeError(message)
    return {
        "recording": str(recording.pk),
        "transcription": str(request.pk),
        "last_version": str(previous.pk),
        "physician": str(physician),
        "encounter": str(encounter.pk),
        "consent": str(consent.pk),
    }


def _retained(
    fixtures: FixtureRows,
    markers: Mapping[str, object],
    transcription: dict[str, str],
    enrollment: object,
    invocations: dict[str, Model],
) -> dict[str, str]:
    """Keep undecided native outputs beside retained historical decisions."""
    physician = UUID(str(markers["practitioner_id"]))
    owner = UUID(str(markers["owner_id"]))
    now = fixtures.now()
    future = now + timedelta(days=30)
    digest = "a" * 64
    template = fixtures.make(
        "ehr.SpecialtyTemplate", key="synthetic", version=1, title="Synthetic"
    )
    document = fixtures.make(
        "ehr.ClinicalDocument", encounter_id=transcription["encounter"]
    )
    final = fixtures.make(
        "ehr.ClinicalDocumentVersion",
        document=document,
        template=template,
        author_id=physician,
        version=1,
        state="finalized",
        finalized_at=now,
        content={"synthetic": True},
        content_digest=digest,
        content_sha256=digest,
    )
    version = fixtures.make(
        "ehr.ClinicalDocumentVersion",
        document=document,
        template=template,
        author_id=physician,
        version=2,
        content={"synthetic": True},
        content_sha256=digest,
    )
    draft = fixtures.make(
        "scribe.AIDraft",
        encounter_id=transcription["encounter"],
        version=version,
        transcript_id=transcription["last_version"],
        consent_id=transcription["consent"],
        invocation=invocations["scribe_draft:succeeded"],
        requested_by_id=physician,
        request_key=fixtures.uuid(),
        generation=1,
        base_revision=1,
        base_epochs=dict.fromkeys(("subjective", "objective", "assessment", "plan"), 0),
        base_content={"synthetic": True},
        sections=["subjective", "objective"],
        source_refs=[{"synthetic": True}],
    )
    suggestions = [
        fixtures.make(
            "scribe.DraftSectionSuggestion",
            version=version,
            draft=draft,
            section=section,
            text="Synthetic pending suggestion.",
            citations=[{"synthetic": True}],
            assertion_class="reported",
        )
        for section in ("subjective", "objective")
    ]
    fixtures.make(
        "scribe.DraftActionProposal",
        version=version,
        draft=draft,
        ordinal=0,
        kind="task",
        text="Synthetic separate proposal.",
        citations=[{"synthetic": True}],
    )
    configuration = fixtures.find("ai.ModelConfiguration", purpose="scribe_draft").pk
    decision = fixtures.make(
        "scribe.ReviewDecision",
        version=version,
        suggestion=suggestions[1],
        reviewed_by_id=physician,
        command_id=fixtures.uuid(),
        request_digest=digest,
        action="rejected",
        section="objective",
        expected_revision=1,
        result_revision=1,
        result_sha256=digest,
        capability_snapshot={
            "purpose": "scribe_draft",
            "capability": "llm_inference",
            "configuration_id": str(configuration),
            "version": 1,
        },
    )
    order = fixtures.make(
        "orders.Order",
        enrollment_id=enrollment,
        ordered_by_id=physician,
        command_id=fixtures.uuid(),
        fingerprint=digest,
    )
    result = fixtures.make("orders.Result")
    result_version = fixtures.make(
        "orders.ResultVersion",
        result=result,
        number=1,
        event_id=fixtures.uuid(),
        fingerprint=digest,
        identity={"synthetic": True},
        storage_key=fixtures.uuid().hex,
        sha256=digest,
        size_bytes=1,
        content_type="application/pdf",
        uploader_id=physician,
    )
    fixtures.make(
        "orders.ResultMatch",
        version=result_version,
        order=order,
        factors={"synthetic": True},
        operator_id=physician,
    )
    job = fixtures.make(
        "ai.ExtractionJob",
        user_id=physician,
        patient_enrollment_id=enrollment,
        invocation=invocations["extraction:succeeded"],
        result_version=result_version,
        sha256=digest,
        rotations=[0],
    )
    observations = [
        fixtures.make(
            "orders.ResultObservation",
            version=result_version,
            position=position,
            source={"synthetic": True},
        )
        for position in range(2)
    ]
    payload = {
        "field": "synthetic",
        "kind": "decimal",
        "raw_text": "Synthetic 1 mg/dL",
        "value": "1",
        "parsed_value": "1",
        "unit": "mg/dL",
        "reference_interval": "",
        "region": {"page": 1, "bbox": [0, 0, 100, 100]},
        "status": "candidate",
        "reason": "",
    }
    candidate = fixtures.make(
        "ai.ExtractionCandidateRecord",
        job=job,
        position=0,
        payload=payload,
        status="candidate",
        expires_at=future,
    )
    fixtures.make(
        "ai.ExtractionCandidateRecord",
        job=job,
        position=1,
        payload=payload,
        status="confirmed",
        observation=observations[0],
        reviewed_by_id=physician,
        reviewed_at=now,
        expires_at=future,
    )
    plan = fixtures.make(
        "workflows.CarePlan",
        enrollment_id=enrollment,
        encounter_id=transcription["encounter"],
        source_version=final,
        clinician_id=physician,
        idempotency_key=fixtures.uuid(),
        fingerprint=digest,
    )
    task = fixtures.make(
        "workflows.Task",
        kind="follow_up",
        subject_ref={"kind": "enrollment", "id": str(enrollment)},
        due_at=future,
        idempotency_key=fixtures.uuid(),
        fingerprint=digest,
    )
    action = fixtures.make(
        "workflows.CareAction",
        plan=plan,
        kind="order",
        window_start=now,
        window_end=future,
        origin_version=final,
        task=task,
        created_by_id=physician,
        idempotency_key=fixtures.uuid(),
        fingerprint=digest,
    )
    care = fixtures.make(
        "ai.CareProposal",
        invocation=invocations["care_coordination:succeeded"],
        plan=plan,
        enrollment_id=enrollment,
        source_version=final,
        plan_revision=1,
        requested_by_id=physician,
        position=0,
        kind="order",
        audience="clinical",
        window_start=now,
        window_end=future,
        state="pending",
    )
    window_start = now.replace(microsecond=0)
    window_end = window_start + timedelta(days=1)
    descriptor = {
        "metric_id": "no_show",
        "version": 1,
        "dimensions": [],
        "filters": {},
        "window": {
            "start": window_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        },
        "tz": "America/Sao_Paulo",
    }
    definition = fixtures.make(
        "analytics.MetricDefinitionVersion",
        metric_id="no_show",
        specification={"synthetic": True},
    )
    fixtures.make(
        "analytics.SavedReport",
        user_id=owner,
        definition=definition,
        descriptor=descriptor,
        window_start=window_start,
        window_end=window_end,
    )
    review = fixtures.make(
        "analytics.AnalystReview",
        user_id=owner,
        invocation=invocations["analyst:reserved"],
        descriptor=descriptor,
        window_start=window_start,
        window_end=window_end,
        authorization_version=digest,
        decision="rejected",
    )
    return {
        "pending_suggestion": str(suggestions[0].pk),
        "review_decision": str(decision.pk),
        "extraction_candidate": str(candidate.pk),
        "result_version": str(result_version.pk),
        "imported_observation": str(observations[0].pk),
        "spare_observation": str(observations[1].pk),
        "care_proposal": str(care.pk),
        "care_action": str(action.pk),
        "analyst_review": str(review.pk),
        "analyst_invocation": str(invocations["analyst:succeeded"].pk),
        "owner": str(owner),
        "physician": str(physician),
        "clinic": str(fixtures.clinic),
    }


def _front_desk(
    fixtures: FixtureRows, policy: Model, enrollment_id: object
) -> list[str]:
    now = fixtures.now()
    digest = "a" * 64
    conversation = fixtures.make(
        "comms.Conversation",
        channel="portal",
        purpose="care",
        enrollment_id=enrollment_id,
        contact_fingerprint=digest,
        contact_address=_CONTRACT["PORTAL_CONTACT"],
        last_activity_at=now,
    )
    thread = fixtures.make("ai.FrontDeskThread", conversation=conversation)
    turns: list[str] = []
    for state in _CONTRACT["PORTAL_TURN_STATES"]:
        inbound = fixtures.make(
            "comms.Message",
            conversation=conversation,
            direction="in",
            body=_CONTRACT["PORTAL_INPUT"],
            event_id=fixtures.uuid().hex,
            created_at=now,
        )
        reply = (
            fixtures.make(
                "comms.Message",
                conversation=conversation,
                direction="out",
                body=_CONTRACT["PORTAL_REPLY"],
                command_id=fixtures.uuid(),
                created_at=now,
            )
            if state == "done"
            else None
        )
        turn = fixtures.make(
            "ai.FrontDeskTurn",
            thread=thread,
            inbound=inbound,
            policy=policy,
            state=state,
            reply=reply,
            intent="greeting" if reply else "",
            outcome="answered" if reply else "",
        )
        turns.append(str(turn.pk))
    return turns


def _configuration(
    fixtures: FixtureRows,
    capability: str,
    purpose: str,
    markers: Mapping[str, object],
    approver: Model,
) -> tuple[Model, Model, Model]:
    now = fixtures.now()
    digest = "a" * 64
    owner = UUID(str(markers["owner_id"]))
    physician = UUID(str(markers["practitioner_id"]))
    config = fixtures.make(
        "ai.ModelConfiguration",
        purpose=purpose,
        capability_key="asr" if purpose == "scribe_asr" else "llm_inference",
        **_CONTRACT["CONFIGURATION"],
    )
    protocol = (
        fixtures.make(
            "ai.EvaluationProtocol",
            capability=capability,
            version=1,
            corpus_version="synthetic-upgrade",
            corpus_sha256=digest,
            case_manifest=[{"id": "synthetic", "encounter": "synthetic"}],
            thresholds_document=json.dumps(
                {
                    "minimum_cases": 1,
                    "minimum_encounters": 1,
                    "maximum_error_rate_ppm": 0,
                    "minimum_citation_support_ppm": 1000000,
                    "minimum_action_correctness_ppm": 1000000,
                }
            ),
            thresholds_sha256=digest,
            registered_by_id=owner,
            clinical_owner_ref="synthetic-not-clinical-approval",
        )
        if purpose != "scribe_draft"
        else fixtures.find(
            "ai.EvaluationProtocol",
            clinic_id=fixtures.clinic,
            capability=capability,
            version=1,
        )
    )
    evaluation = fixtures.make(
        "ai.EvaluationRun",
        protocol=protocol,
        configuration=config,
        configuration_version=1,
        versions={"prompt": digest, "retrieval": digest, "schema": digest},
        rater_one_id=owner,
        rater_two_id=physician,
        adjudicator_id=approver.pk,
        created_by_id=owner,
        status="passed",
        scored_at=now,
        finished_at=now,
        sample_results={"synthetic": True},
    )
    for slot, human in (("rater_one", owner), ("rater_two", physician)):
        fixtures.make(
            "ai.EvaluationRating",
            run=evaluation,
            case_id="synthetic",
            slot=slot,
            user_id=human,
            labels=dict.fromkeys(
                (
                    "critical_error",
                    "unsupported_citation",
                    "incorrect_action",
                    "policy_violation",
                    "unauthorized_effect",
                    "duplicate_effect",
                    "missed_escalation",
                ),
                0,
            ),
        )
    approval = fixtures.make(
        "ai.ModelChangeRequest",
        configuration=config,
        run=evaluation,
        requested_by_id=owner,
        approved_by_id=approver.pk,
        status="approved",
        approval_ref="synthetic-upgrade",
        decided_at=now,
    )
    return config, approval, _pending_evaluation(fixtures, config, protocol, markers)


def _pending_evaluation(
    fixtures: FixtureRows,
    configuration: Model,
    protocol: Model,
    markers: Mapping[str, object],
) -> Model:
    raters = []
    for _slot in range(2):
        human = fixtures.make(
            "identity.User", username="upgrade-rater-" + fixtures.uuid().hex
        )
        fixtures.make("identity.UserClinicRole", user=human, role="physician")
        raters.append(human)
    digest = "a" * 64
    return fixtures.make(
        "ai.EvaluationRun",
        protocol=protocol,
        configuration=configuration,
        configuration_version=1,
        versions={
            "prompt": digest,
            "retrieval": digest,
            "schema": digest,
            "model": digest,
            "version": "1",
        },
        rater_one_id=markers["practitioner_id"],
        rater_two=raters[0],
        adjudicator=raters[1],
        created_by_id=markers["owner_id"],
        status="pending",
    )


def _auxiliary_guards(fixtures: FixtureRows, config: Model, owner: UUID) -> None:
    now = fixtures.now()
    fixtures.make("ai.AICircuit", configuration=config, opened_at=now, closed_at=now)
    fixtures.make(
        "ai.KnowledgeArticle",
        key="synthetic",
        version=1,
        topic="hours",
        title="Synthetic",
        body="Synthetic hours.",
        keywords=["synthetic"],
        created_by_id=owner,
    )


def _execution(
    fixtures: FixtureRows, proposal: Model, approver: Model, digest: str
) -> Model:
    now = fixtures.now()
    fixtures.make(
        "ai.Approval",
        proposal=proposal,
        approver_id=approver.pk,
        digest=digest,
        authority_snapshot={"synthetic": True},
        step_up_evidence_ref=fixtures.uuid(),
        step_up_evidence={"synthetic": True},
        attestation=digest,
        step_up_verified_at=now,
    )
    execution = fixtures.make(
        "ai.ActionExecution",
        proposal=proposal,
        digest=digest,
        provider_key=fixtures.uuid(),
        intent_at=now,
        deadline=now + timedelta(days=30),
    )
    fixtures.make(
        "ai.ActionReceipt",
        execution=execution,
        kind="reconciliation",
        outcome="unknown",
        provider_reference=fixtures.uuid(),
        digest=digest,
        fencing_token=1,
        attestation=digest,
    )
    fixtures.make(
        "ai.Compensation",
        execution=execution,
        provider_key=fixtures.uuid(),
        reason_code="synthetic",
    )
    return execution


def declare_ai_rows(
    fixtures: FixtureRows, markers: Mapping[str, object], enrollment_id: object
) -> dict[str, object]:
    """Describe the AI fixture graph using only the supplied row backend."""
    owner = UUID(str(markers["owner_id"]))
    physician = UUID(str(markers["practitioner_id"]))
    digest = "a" * 64
    approver = fixtures.make(
        "identity.User", username="upgrade-ai-" + fixtures.uuid().hex
    )
    fixtures.make("identity.UserClinicRole", user_id=approver.pk, role="owner")
    fixtures.make("identity.UserClinicRole", user_id=owner, role="physician")
    fixtures.make("identity.UserClinicRole", user_id=owner, role="finance")
    fixtures.make("identity.UserClinicRole", user_id=owner, role="clinic_admin")
    fixtures.make(
        "ai.GatewaySwitch", id=1, enabled=True, approved_regions=["br-synthetic"]
    )
    invocations: dict[str, Model] = {}
    approvals: list[str] = []
    pending_evaluations: list[str] = []
    runs: list[str] = []
    proposals: list[str] = []
    executions: list[str] = []
    for capability, purpose in _CONTRACT["HISTORIES"]:
        config, approval, pending = _configuration(
            fixtures, capability, purpose, markers, approver
        )
        approvals.append(str(approval.pk))
        pending_evaluations.append(str(pending.pk))
        invocations.update(
            _invocations(
                fixtures,
                config,
                purpose,
                (
                    owner
                    if purpose in {"front_desk", "revenue_cycle", "analyst"}
                    else physician
                ),
                enrollment_id,
            )
        )
        if purpose in {"scribe_asr", "scribe_draft", "extraction", "analyst"}:
            continue
        policy = fixtures.make(
            "ai.AgentPolicyVersion",
            capability=purpose,
            version=1,
            autonomy_ceiling="A1",
            tool_allowlist=[],
            budget_actions=1,
            budget_micros=100,
            timeout_seconds=30,
            escalation_queue="administrative",
            compensation_policy={"mode": "manual", "version": 1},
            enabled=True,
            created_by_id=owner,
        )
        if purpose == "front_desk":
            front_policy = policy
            continue
        for state in ("validated", "approved"):
            run = fixtures.make(
                "ai.AgentRun",
                policy=policy,
                user_id=owner,
                configuration=config,
                invocation=invocations[purpose + ":succeeded"],
                prompt_sha256=digest,
                contract={"synthetic": True},
                evaluation_set="synthetic-upgrade",
            )
            runs.append(str(run.pk))
            proposal = fixtures.make(
                "ai.ActionProposal",
                run=run,
                payload='{"synthetic":true}',
                digest=(proposal_digest := fixtures.uuid().hex + fixtures.uuid().hex),
                idempotency_key=fixtures.uuid(),
                state=state,
                quorum=1,
            )
            proposals.append(str(proposal.pk))
            if state == "approved":
                executions.append(
                    str(_execution(fixtures, proposal, approver, proposal_digest).pk)
                )
    _auxiliary_guards(fixtures, config, owner)
    turns = _front_desk(fixtures, front_policy, enrollment_id)
    transcription = _transcription(
        fixtures, markers, enrollment_id, invocations["scribe_asr:succeeded"]
    )
    retained = _retained(fixtures, markers, transcription, enrollment_id, invocations)
    return {
        "approvals": approvals,
        "pending_evaluations": pending_evaluations,
        "runs": runs,
        "proposals": proposals,
        "turns": turns,
        "executions": executions,
        "retained": retained,
        **transcription,
        "invocations": {key: str(value.pk) for key, value in invocations.items()},
    }
