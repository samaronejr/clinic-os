"""Owner-loaded synthetic historical AI fixtures, not runtime approval authority.

The released base models supply defaults and envelope encryption. Only user
triggers are suspended while loading the fixture; CHECK/UNIQUE/FK constraints
remain active. Trigger suspension and rows share one atomic transaction, so a
failed load rolls both back. Every trigger is enabled before snapshot/upgrade.
No guard is disabled during migration or any forward assertion.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from django.apps import apps
from django.db import connection, transaction
from django.db.migrations.state import ModelState
from django.utils import timezone
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import ClassVar

    from django.db.models import Manager, Model


class _HistoricalModel(Protocol):
    objects: ClassVar[Manager[Model]]


class LegacyRows:
    """Build one coherent old-schema graph without importing candidate models."""

    def __init__(self, organization: UUID, clinic: UUID) -> None:
        """Bind every loaded tenant row to the existing representative clinic."""
        self.organization = organization
        self.clinic = clinic
        self.tables: dict[str, list[str]] = {}

    def make(self, label: str, **fields: object) -> Model:
        """Load a historical row with constraints active and its trigger restored."""
        model = apps.get_model(label)
        state = ModelState.from_model(model)
        table = str(
            state.options.get("db_table", state.app_label + "_" + state.name.lower())
        )
        if table not in self.tables:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass "
                    "AND NOT tgisinternal AND tgenabled<>'O'",
                    ["clinic_app." + table],
                )
                if cursor.fetchone() != (0,):
                    message = "historical fixture requires enabled user triggers"
                    raise RuntimeError(message)
                cursor.execute(
                    sql.SQL("ALTER TABLE clinic_app.{} DISABLE TRIGGER USER").format(
                        sql.Identifier(table)
                    )
                )
                self.tables[table] = [
                    field.name
                    for field in connection.introspection.get_table_description(
                        cursor, table
                    )
                ]
        if "organization" in state.fields:
            fields.setdefault("organization_id", self.organization)
        if "clinic" in state.fields:
            fields.setdefault("clinic_id", self.clinic)
        manager = cast("type[_HistoricalModel]", model).objects
        return manager.create(**fields)

    def finish(self) -> dict[str, object]:
        """Restore guards and record every original column, including ciphertext."""
        snapshots: dict[str, object] = {}
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, columns in self.tables.items():
                cursor.execute(
                    sql.SQL("ALTER TABLE clinic_app.{} ENABLE TRIGGER USER").format(
                        sql.Identifier(table)
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "SELECT id::text,to_jsonb(t) FROM clinic_app.{} t ORDER BY id"
                    ).format(sql.Identifier(table))
                )
                rows = {
                    key: hashlib.sha256(
                        json.dumps(json.loads(value), sort_keys=True).encode()
                    ).hexdigest()
                    for key, value in cursor.fetchall()
                }
                snapshots[table] = {
                    "columns": columns,
                    "rows": rows,
                    "count": len(rows),
                }
        return snapshots


def _invocations(
    fixtures: LegacyRows,
    configuration: Model,
    purpose: str,
    user: UUID,
    enrollment_id: object,
) -> dict[str, Model]:
    now = timezone.now()
    future = now + timedelta(days=30)
    digest = "a" * 64
    result: dict[str, Model] = {}
    for status in ("reserved", "succeeded"):
        source = fixtures.make(
            "ai.AIArtifact",
            user_id=user,
            purpose=purpose,
            patient_enrollment_id=(
                None if purpose in {"front_desk", "revenue_cycle"} else enrollment_id
            ),
            kind="input",
            method="transcribe" if purpose == "scribe_asr" else "complete",
            content="Synthetic upgrade input; no clinical data.",
            retention_class="transient_input",
            expires_at=future,
        )
        operation_id = uuid4()
        invocation_id = uuid4()
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
            idempotency_key=uuid4(),
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
                method="transcribe" if purpose == "scribe_asr" else "complete",
                invocation=invocation,
                content="Synthetic retained output.",
                retention_class="review_draft",
                expires_at=future,
            )
    return result


def _transcription(
    fixtures: LegacyRows,
    markers: Mapping[str, object],
    enrollment_id: object,
    invocation: Model,
) -> dict[str, str]:
    patient = UUID(str(markers["patient_id"]))
    owner = UUID(str(markers["owner_id"]))
    physician = UUID(str(markers["practitioner_id"]))
    now = timezone.now()
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
        capability_version_id=uuid4(),
        capability_snapshot={"synthetic": True},
        token_digest=digest,
        creation_key=uuid4(),
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
        storage_key=uuid4().hex,
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
    source_id = uuid4()
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
    }


def _front_desk(
    fixtures: LegacyRows, policy: Model, enrollment_id: object
) -> list[str]:
    now = timezone.now()
    digest = "a" * 64
    conversation = fixtures.make(
        "comms.Conversation",
        channel="portal",
        purpose="care",
        enrollment_id=enrollment_id,
        contact_fingerprint=digest,
        contact_address="synthetic.invalid",
        last_activity_at=now,
    )
    thread = fixtures.make("ai.FrontDeskThread", conversation=conversation)
    turns: list[str] = []
    for state in ("pending", "done"):
        inbound = fixtures.make(
            "comms.Message",
            conversation=conversation,
            direction="in",
            body="Synthetic inbound.",
            event_id=uuid4().hex,
            created_at=now,
        )
        reply = (
            fixtures.make(
                "comms.Message",
                conversation=conversation,
                direction="out",
                body="Synthetic reply.",
                command_id=uuid4(),
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


def _baseline_approvals() -> None:
    """Prove the restored base guards regard the old approvals as usable."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT bool_and(clinic_app.ai_eval_activation_allowed("
            "configuration_id,clinic_id)) FROM clinic_app.ai_modelchangerequest"
        )
        if cursor.fetchone() != (True,):
            message = "historical approvals do not authorize the base configuration"
            raise RuntimeError(message)
        cursor.execute("RESET ROLE")


def seed_ai(markers: Mapping[str, object]) -> dict[str, object]:
    """Retain pre-rollout approvals, pending work, outputs and ASR provenance."""
    organization = UUID(str(markers["organization_id"]))
    owner = UUID(str(markers["owner_id"]))
    physician = UUID(str(markers["practitioner_id"]))
    patient = UUID(str(markers["patient_id"]))
    now = timezone.now()
    future = now + timedelta(days=30)
    digest = "a" * 64
    fixtures = LegacyRows(organization, UUID(str(markers["clinic_id"])))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(organization), str(owner)],
        )
        enrollment = apps.get_model("intake.PatientClinicEnrollment")
        manager = cast("type[_HistoricalModel]", enrollment).objects
        enrollment_id = manager.get(patient_id=patient, clinic_id=fixtures.clinic).pk
        approver = fixtures.make("identity.User", username="upgrade-ai-" + uuid4().hex)
        fixtures.make("identity.UserClinicRole", user_id=approver.pk, role="owner")
        fixtures.make("identity.UserClinicRole", user_id=owner, role="physician")
        fixtures.make("identity.UserClinicRole", user_id=owner, role="finance")
        fixtures.make("identity.UserClinicRole", user_id=owner, role="clinic_admin")
        fixtures.make(
            "ai.GatewaySwitch", id=1, enabled=True, approved_regions=["synthetic"]
        )
        invocations: dict[str, Model] = {}
        approvals: list[str] = []
        runs: list[str] = []
        proposals: list[str] = []
        executions: list[str] = []
        labels = dict.fromkeys(
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
        )
        for capability, purpose in (
            ("AI-04", "front_desk"),
            ("AI-05", "care_coordination"),
            ("AI-06", "revenue_cycle"),
            ("AI-01", "scribe_asr"),
        ):
            config = fixtures.make(
                "ai.ModelConfiguration",
                purpose=purpose,
                capability_key="asr" if purpose == "scribe_asr" else "llm_inference",
                provider="synthetic",
                model_id="upgrade-synthetic",
                region="synthetic",
                jurisdiction_class="BR",
                endpoint="https://ai.synthetic.invalid/",
                retention_terms="synthetic",
                training_terms="synthetic",
                version=1,
                route="primary",
                state="active",
                approval_ref="synthetic",
                prompt_sha256=digest,
                retrieval_sha256=digest,
                schema_sha256=digest,
            )
            protocol = fixtures.make(
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
                    labels=labels,
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
            approvals.append(str(approval.pk))
            invocations.update(
                _invocations(
                    fixtures,
                    config,
                    purpose,
                    owner if purpose in {"front_desk", "revenue_cycle"} else physician,
                    enrollment_id,
                )
            )
            if purpose == "scribe_asr":
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
                    digest=uuid4().hex + uuid4().hex,
                    idempotency_key=uuid4(),
                    state=state,
                    quorum=1,
                )
                proposals.append(str(proposal.pk))
                if state == "approved":
                    execution = fixtures.make(
                        "ai.ActionExecution",
                        proposal=proposal,
                        digest=uuid4().hex + uuid4().hex,
                        provider_key=uuid4(),
                        intent_at=now,
                        deadline=future,
                    )
                    executions.append(str(execution.pk))
        turns = _front_desk(fixtures, front_policy, enrollment_id)
        transcription = _transcription(
            fixtures, markers, enrollment_id, invocations["scribe_asr:succeeded"]
        )
        snapshots = fixtures.finish()
        _baseline_approvals()
    return {
        "tables": snapshots,
        "approvals": approvals,
        "runs": runs,
        "proposals": proposals,
        "turns": turns,
        "executions": executions,
        **transcription,
        "invocations": {key: str(value.pk) for key, value in invocations.items()},
        "fixture_trigger_policy": (
            "owner insert-only suspension; all enabled before upgrade"
        ),
    }
